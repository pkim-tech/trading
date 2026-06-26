import sys
import argparse
import logging
import pandas as pd
import matplotlib
# Force non-GUI backend so it saves cleanly without X11 server warnings
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

# Import our modular engines
from trading_engine import process_indicators, CACHE_DIR, check_signal

def parse_visual_arguments():
    """Configures command-line arguments to let you pick which sim to see."""
    parser = argparse.ArgumentParser(description="Trading Strategy Visualizer Audit Tool")
    parser.add_argument("--ticker", type=str, default=None, help="Specific ticker symbol to audit")
    parser.add_argument("--window", type=int, default=10, help="SMA lookback window size")
    parser.add_argument("--tp", type=float, default=0.05, help="Take Profit ratio (e.g. 0.05 for 5%)")
    parser.add_argument("--sl", type=float, default=0.02, help="Stop Loss ratio (e.g. 0.02 for 2%)")
    return parser.parse_known_args()

def generate_trade_chart(ticker=None, window=10, tp=0.05, sl=0.02):
    args, unknown = parse_visual_arguments()
    
    # Prioritize arguments passed programmatically (e.g. from the sweep loop), fall back to CLI flags
    final_window = window if window != 10 else args.window
    final_tp = tp if tp != 0.05 else args.tp
    final_sl = sl if sl != 0.02 else args.sl
    
    # 1. DETECT AND LOAD CACHE DATA
    cached_files = list(Path("./cache").glob("*_1h.csv"))
    filtered_assets = [f for f in cached_files if "SPY" not in f.name]
    
    if not filtered_assets:
        print("❌ No hourly CSV data found in ./cache/")
        sys.exit(1)
        
    if ticker is not None:
        final_ticker = ticker.upper()
    elif args.ticker is not None:
        final_ticker = args.ticker.upper()
    else:
        final_ticker = filtered_assets[0].name.split('_')[0]
    
    cache_path = CACHE_DIR / f"{final_ticker}_1h.csv"
    if not cache_path.exists():
        print(f"❌ Target asset cache missing: {cache_path}")
        return

    print("\n" + "="*60)
    print(f"🔎 GENERATING AUDIT GRAPH FOR {final_ticker}")
    print(f"   Settings -> Window: {final_window} | TP: {final_tp*100}% | SL: {final_sl*100}%")
    print("="*60)
    
    df_hourly = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    
    # 2. GENERATE DAILY INDICATORS
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    df_daily_processed = process_indicators(df_daily, final_window)
    
    # 3. SET UP BASE CHART
    plt.figure(figsize=(16, 9))
    plt.plot(df_hourly.index, df_hourly[close_col], label=f"{final_ticker} Hourly Close", color="#1f77b4", alpha=0.5, linewidth=1.5)
    
    # Map Daily Indicators cleanly to the Hourly Timeline for smooth plotting
    df_indicators_hourly = df_daily_processed[['SMA', 'Std']].reindex(df_hourly.index, method='ffill')
    
    # Calculate Upper & Lower Bollinger Bands (2.0 Entry Threshold)
    df_indicators_hourly['Upper_Band'] = df_indicators_hourly['SMA'] + (df_indicators_hourly['Std'] * 2.0)
    df_indicators_hourly['Lower_Band'] = df_indicators_hourly['SMA'] - (df_indicators_hourly['Std'] * 2.0)
    
    # Plot Bollinger Strategy Bands
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['SMA'], label="Daily SMA", color="#ff7f0e", linestyle="--", alpha=0.7)
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['Upper_Band'], label="Upper Threshold (+2 Std)", color="purple", linestyle=":", alpha=0.5)
    plt.plot(df_indicators_hourly.index, df_indicators_hourly['Lower_Band'], label="Lower Threshold (-2 Std)", color="purple", linestyle=":", alpha=0.5)
    plt.fill_between(df_indicators_hourly.index, df_indicators_hourly['Lower_Band'], df_indicators_hourly['Upper_Band'], color='purple', alpha=0.03)

    # 4. TRADING SIMULATION LOOP & PLOT MARKERS
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
                
                signal = check_signal(current_price, sma, std)
                if signal == "BUY":
                    position = "LONG"
                    entry_price = current_price
                    buy_x.append(timestamp)
                    buy_y.append(current_price)
                    
        elif position == "LONG":
            price_return = (current_price - entry_price) / entry_price
            
            if price_return >= final_tp:
                position = "CASH"
                win_x.append(timestamp)
                win_y.append(current_price)
                closed_trades.append({"Result": "WIN", "Return": price_return})
            elif price_return <= -final_sl:
                position = "CASH"
                loss_x.append(timestamp)
                loss_y.append(current_price)
                closed_trades.append({"Result": "LOSS", "Return": price_return})

    # Plot matched scatter coordinates using high contrast markers
    if buy_x:
        plt.scatter(buy_x, buy_y, color="#2ca02c", marker="^", s=130, edgecolor='black', zorder=5, label="BUY ENTRY")
    if win_x:
        plt.scatter(win_x, win_y, color="#17becf", marker="v", s=130, edgecolor='black', zorder=5, label="TAKE PROFIT (WIN)")
    if loss_x:
        plt.scatter(loss_x, loss_y, color="#d62728", marker="v", s=130, edgecolor='black', zorder=5, label="STOP LOSS (LOSS)")

    # 5. SCORECARD TERMINAL REPORT (WITH BENCHMARK ALIGNMENT)
    asset_bh_return = ((df_hourly[close_col].iloc[-1] - df_hourly[close_col].iloc[0]) / df_hourly[close_col].iloc[0]) * 100
    
    spy_cache = CACHE_DIR / "SPY_1h.csv"
    if spy_cache.exists():
        spy_df = pd.read_csv(spy_cache, index_col=0, parse_dates=True).sort_index()
        spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
        spy_sliced = spy_df.loc[df_hourly.index.min():df_hourly.index.max()]
        spy_bh_return = ((spy_sliced[spy_col].iloc[-1] - spy_sliced[spy_col].iloc[0]) / spy_sliced[spy_col].iloc[0]) * 100 if len(spy_sliced) > 0 else 0.0
    else:
        spy_bh_return = 0.0

    print(f"\n📊 PERFORMANCE SUMMARY FOR {final_ticker}:")
    print(f"  🔹 Total Closed Trades: {len(closed_trades)}")
    
    strategy_growth = 0.0
    if closed_trades:
        df_tr = pd.DataFrame(closed_trades)
        wins = len(df_tr[df_tr['Result'] == 'WIN'])
        losses = len(df_tr[df_tr['Result'] == 'LOSS'])
        strategy_growth = ((df_tr['Return'] + 1).prod() - 1) * 100
        print(f"  🟢 Wins:  {wins}  |  🔴 Losses: {losses}")
    
    print("-" * 60)
    print(f"  🤖 Your Z-Score Strategy:   {strategy_growth:+.2f}%")
    print(f"  🪨 Buy & Hold {final_ticker} Asset:  {asset_bh_return:+.2f}%")
    print(f"  🇺🇸 S&P 500 Index (SPY):     {spy_bh_return:+.2f}%")
    print("-" * 60)
    print(f"  🚀 True Alpha vs SPY Market: {(strategy_growth - spy_bh_return):+.2f}%")
    print("="*60)

    # Finalize visual styling
    plt.title(f"Visual Strategy Audit: {final_ticker} | Window: {final_window} | TP: {final_tp*100}% | SL: {final_sl*100}%", fontsize=14, fontweight='bold')
    plt.xlabel("Timeline Date")
    plt.ylabel("Execution Price ($)")
    plt.grid(True, alpha=0.15)
    plt.legend(loc="upper left")
    
    output_img = f"audit_{final_ticker}_w{final_window}.png"
    plt.savefig(output_img, bbox_inches='tight', dpi=150)
    print(f"\n💾 Visual blueprint generated: ./{output_img}\n")


def run_backtest_simulation(df_hourly, df_daily_indicators, ticker, 
                            mode="BACKTEST", target_hours=(9, 14),
                            take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28):
    """Executes historical simulation passes using broad risk boundaries 
    and a robust bar-row counter that naturally respects weekends and holidays.
    Splits time-based decays cleanly into TWIN or TLOSS categories.
    """
    trades = []
    active_trade = None
    
    # Pre-map daily indicator dates to hourly timestamps for ultra-fast lookup
    df_hourly = df_hourly.copy()
    df_hourly['date_str'] = df_hourly.index.strftime('%Y-%m-%d')
    
    for i in range(len(df_hourly)):
        current_time = df_hourly.index[i]
        current_price = df_hourly['Close'].iloc[i]
        current_date_str = df_hourly['date_str'].iloc[i]
        
        # ---------------------------------------------------------------------
        # STEP 1: EVALUATE ACTIVE TRADES (EXIT MONITORING)
        # ---------------------------------------------------------------------
        if active_trade:
            active_trade['hours_held'] += 1 # Accumulate raw historical market rows
            price_change = (current_price - active_trade['Entry Price']) / active_trade['Entry Price']
            
            # Trigger A: Take Profit Hit
            if price_change >= take_profit:
                active_trade['Exit Price'] = current_price
                active_trade['Exit Time'] = current_time
                active_trade['Result'] = "WIN"
                active_trade['Return'] = price_change
                trades.append(active_trade)
                active_trade = None
                continue
                
            # Trigger B: Structural Wide Safety Stop Loss Hit
            elif price_change <= -stop_loss:
                active_trade['Exit Price'] = current_price
                active_trade['Exit Time'] = current_time
                active_trade['Result'] = "LOSS"
                active_trade['Return'] = price_change
                trades.append(active_trade)
                active_trade = None
                continue
                
            # Trigger C: Time Decay Escape Hatch (Differentiates into TWIN / TLOSS buckets)
            elif active_trade['hours_held'] >= max_hours_to_hold:
                active_trade['Exit Price'] = current_price
                active_trade['Exit Time'] = current_time
                active_trade['Return'] = price_change
                active_trade['Result'] = "TWIN" if price_change > 0 else "TLOSS"
                
                trades.append(active_trade)
                active_trade = None
                continue
                
            # If no exit triggers hit, hold position and proceed to next hour
            continue

        # ---------------------------------------------------------------------
        # STEP 2: EVALUATE MARKET SIGNALS (ENTRY MONITORING)
        # ---------------------------------------------------------------------
        if current_time.hour not in target_hours:
            continue
            
        if current_date_str not in df_daily_indicators.index.strftime('%Y-%m-%d'):
            continue
            
        prior_day_data = df_daily_indicators.loc[df_daily_indicators.index.strftime('%Y-%m-%d') == current_date_str].iloc[0]
        
        if 'SMA' in prior_day_data and 'Std' in prior_day_data:
            lower_band = prior_day_data['SMA'] - (prior_day_data['Std'] * 2.0)
            
            if 'Trend_Filter' in prior_day_data:
                entry_signal = (current_price <= lower_band) and (current_price > prior_day_data['Trend_Filter'])
            else:
                entry_signal = (current_price <= lower_band)
                
            if entry_signal:
                active_trade = {
                    "Ticker": ticker,
                    "Entry Time": current_time,
                    "Entry Price": current_price,
                    "Exit Time": None,
                    "Exit Price": None,
                    "hours_held": 0,
                    "Result": "OPEN",
                    "Return": 0.0
                }
                
    # Safety Check: Handle ongoing open positions that hit the edge of historical files
    if active_trade:
        active_trade['Exit Price'] = df_hourly['Close'].iloc[-1]
        active_trade['Exit Time'] = df_hourly.index[-1]
        active_trade['Result'] = "OPEN"
        active_trade['Return'] = (active_trade['Exit Price'] - active_trade['Entry Price']) / active_trade['Entry Price']
        trades.append(active_trade)

    return trades

if __name__ == "__main__":
    generate_trade_chart()