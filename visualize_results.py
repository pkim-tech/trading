import sys
import argparse
import logging
import pandas as pd
import matplotlib
# Force non-GUI backend for head-less/CLI environments
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

# Import our modular engines
from trading_engine import process_indicators, CACHE_DIR, check_signal

def parse_visual_arguments():
    """Restores flexible manual arguments alongside your automatic lookup fallback."""
    parser = argparse.ArgumentParser(description="Trading Strategy Visualizer Audit Tool")
    parser.add_argument("--ticker", type=str, default=None, help="Specific ticker symbol to audit")
    parser.add_argument("--window", type=int, default=10, help="SMA lookback window size")
    parser.add_argument("--tp", type=float, default=0.05, help="Take Profit ratio (e.g. 0.05 for 5%)")
    parser.add_argument("--sl", type=float, default=0.02, help="Stop Loss ratio (e.g. 0.02 for 2%)")
    return parser.parse_known_args()

def generate_trade_chart(ticker="AAPL", window=10, tp=0.05, sl=0.02):
    print(f"📈 Generating interactive visual chart for {ticker} (Window: {window})...")
    
    # 1. LOAD CACHE DATA
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    if not cache_path.exists():
        print(f"❌ Cache file missing for {ticker} at {cache_path}!")
        return
        
    df_hourly = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    
    # 2. GENERATE INDICATORS
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    df_daily_processed = process_indicators(df_daily, window)
    
    # 3. SET UP MATPLOTLIB CHART
    plt.figure(figsize=(14, 7))
    plt.plot(df_hourly.index, df_hourly[close_col], label=f"{ticker} Hourly Close", color="#1f77b4", alpha=0.6)
    
    # Upsample/interpolate daily bands back onto the hourly index cleanly
    df_indicators_hourly = df_daily_processed[['SMA', 'Std']].reindex(df_hourly.index, method='ffill')
    df_indicators_hourly['Upper_Band'] = df_indicators_hourly['SMA'] + (df_indicators_hourly['Std'] * 2.0)
    df_indicators_hourly['Lower_Band'] = df_indicators_hourly['SMA'] - (df_indicators_hourly['Std'] * 2.0)
    
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['SMA'], label=f"{window}-Day SMA", color="#ff7f0e", linestyle="--")
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['Upper_Band'], label="Upper Band (+2 Std)", color="purple", linestyle=":", alpha=0.4)
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['Lower_Band'], label="Lower Band (-2 Std)", color="purple", linestyle=":", alpha=0.4)
    plt.fill_between(df_indicators_hourly.index, df_indicators_hourly['Lower_Band'], df_indicators_hourly['Upper_Band'], color='purple', alpha=0.02)

    # 4. OVERLAY EXECUTION ARROWS (RUNNING STRATEGY SIMULATION)
    position = "CASH"
    entry_price = 0.0
    closed_trades = []
    
    buy_x, buy_y = [], []
    win_x, win_y = [], []
    loss_x, loss_y = [], []

    for timestamp, row in df_hourly.iterrows():
        current_price = row[close_col]
        current_date = timestamp.date()
        
        if position == "CASH":
            if timestamp.hour in (9, 14):
                try:
                    completed_days = df_daily_processed[:str(current_date)]
                    if len(completed_days) < 2: continue
                    prior_day = completed_days.iloc[-2]
                    sma, std = prior_day['SMA'], prior_day['Std']
                except (IndexError, KeyError): continue
                
                if check_signal(current_price, sma, std) == "BUY":
                    position = "LONG"
                    entry_price = current_price
                    buy_x.append(timestamp)
                    buy_y.append(current_price)
                    
        elif position == "LONG":
            price_return = (current_price - entry_price) / entry_price
            
            if price_return >= tp:
                position = "CASH"
                win_x.append(timestamp)
                win_y.append(current_price)
                closed_trades.append({"Result": "WIN", "Return": price_return})
            elif price_return <= -sl:
                position = "CASH"
                loss_x.append(timestamp)
                loss_y.append(current_price)
                closed_trades.append({"Result": "LOSS", "Return": price_return})

    # Plot matched scatter coordinates natively onto the active timeline window
    if buy_x:
        plt.scatter(buy_x, buy_y, color="green", marker="^", s=130, edgecolor='black', zorder=5, label="BUY ENTRY")
    if win_x:
        plt.scatter(win_x, win_y, color="cyan", marker="v", s=130, edgecolor='black', zorder=5, label="TAKE PROFIT (WIN)")
    if loss_x:
        plt.scatter(loss_x, loss_y, color="red", marker="v", s=130, edgecolor='black', zorder=5, label="STOP LOSS (LOSS)")

    print(f"📊 Found {len(closed_trades)} completed strategy trades to map onto timeline canvas.")

    # 5. INTEGRATE HISTORICAL PERFORMANCE CONTROL STATS
    asset_bh_return = ((df_hourly[close_col].iloc[-1] - df_hourly[close_col].iloc[0]) / df_hourly[close_col].iloc[0]) * 100
    
    spy_cache = CACHE_DIR / "SPY_1h.csv"
    if spy_cache.exists():
        spy_df = pd.read_csv(spy_cache, index_col=0, parse_dates=True).sort_index()
        spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
        spy_sliced = spy_df.loc[df_hourly.index.min():df_hourly.index.max()]
        spy_bh_return = ((spy_sliced[spy_col].iloc[-1] - spy_sliced[spy_col].iloc[0]) / spy_sliced[spy_col].iloc[0]) * 100 if len(spy_sliced) > 0 else 0.0
    else:
        spy_bh_return = 0.0

    strategy_growth = ((pd.DataFrame(closed_trades)['Return'] + 1).prod() - 1) * 100 if closed_trades else 0.0
    
    print("\n" + "="*60)
    print(f"📊 PERFORMANCE VS. BENCHMARK SCORECARD FOR {ticker}:")
    print(f"  🤖 Your Z-Score Strategy:   {strategy_growth:+.2f}%")
    print(f"  🪨 Buy & Hold {ticker} Asset:  {asset_bh_return:+.2f}%")
    print(f"  🇺🇸 S&P 500 Index (SPY):     {spy_bh_return:+.2f}%")
    print(f"  🚀 Alpha vs SPY Market:     {(strategy_growth - spy_bh_return):+.2f}%")
    print("="*60 + "\n")

    # Finalize visual frame
    plt.title(f"Visual Trade Breakdown: {ticker} (TP: {tp*100}%, SL: {sl*100}%)")
    plt.xlabel("Date")
    plt.ylabel("Price ($)")
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left")
    
    # Save chart image to workspace filesystem
    output_image = f"trade_visualization_{ticker}.png"
    plt.savefig(output_image, bbox_inches='tight', dpi=150)
    print(f"💾 Visualization saved successfully to: ./{output_image}")
    
    try:
        plt.show()
    except Exception as e:
        print(f"ℹ️ Native terminal display environment non-interactive, skipping pop-up window: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    args, unknown = parse_visual_arguments()
    
    # Process ticker assignment override priority logic
    if args.ticker:
        ticker_to_run = args.ticker.upper()
    else:
        cached_files = list(Path("./cache").glob("*_1h.csv"))
        filtered_assets = [f for f in cached_files if "SPY" not in f.name]
        ticker_to_run = filtered_assets[0].name.split('_')[0] if filtered_assets else "AAPL"
    
    generate_trade_chart(
        ticker=ticker_to_run, 
        window=args.window, 
        tp=args.tp, 
        sl=args.sl
    )