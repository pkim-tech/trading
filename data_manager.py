import os
import time
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime

# Create a local directory named 'cache' to store data files
CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(exist_ok=True)

def generate_synthetic_data(days=60, points_per_day=7, base_price=150.0):
    """
    Generates fake market data in memory for offline development.
    Zero internet required.
    """
    import numpy as np
    total_hourly_points = days * points_per_day
    hourly_ticks = pd.date_range(start="2026-01-01", periods=total_hourly_points, freq="h")
    x = np.linspace(0, 4 * np.pi, total_hourly_points)
    hourly_prices = base_price + (np.sin(x) * 5) + np.random.normal(0, 1, total_hourly_points)
    
    df_hourly = pd.DataFrame(index=hourly_ticks)
    df_hourly['Adj Close'] = hourly_prices
    df_daily = df_hourly.resample('D').last().dropna()
    return df_daily, df_hourly


def fetch_live_data_smart(ticker):
    """
    Hardened Incremental Backfiller:
    1. Loads local CSV cache if it exists.
    2. Measures elapsed days dynamically.
    3. Requests an overlapping buffer window from Yahoo to catch missed holidays/weekends.
    4. Automatically de-duplicates and updates old rows with Yahoo's freshest data.
    """
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    now = datetime.now()
    
    # --- STEP 1: INITIAL BOOTSTRAP (No local file exists yet) ---
    if not cache_path.exists():
        print(f"🌐 No cache found. Performing initial maximum history pull for {ticker}...")
        try:
            # 730 days is Yahoo's hard limit for hourly history
            df_new = yf.download(ticker, period="730d", interval="1h")
            
            if df_new.empty:
                print(f"❌ Error: Yahoo Finance returned no data for {ticker}.")
                return None, None
                
            # Flatten MultiIndex columns if present in newer yfinance versions
            if isinstance(df_new.columns, pd.MultiIndex):
                df_new.columns = df_new.columns.get_level_values(0)
                
            # Explicitly force datetime index and remove timezone info for uniform storage
            df_new.index = pd.to_datetime(df_new.index).tz_localize(None)
            df_new.index.name = "Datetime"
                
            df_new.to_csv(cache_path)
            print(f"💾 Initial 2-year history cached for {ticker}.")
            
            df_daily = df_new.resample('D').last().dropna()
            return df_daily, df_new
            
        except Exception as e:
            print(f"❌ Failed to initialize live data for {ticker}: {e}")
            return None, None

    # --- STEP 2: LOAD & INSPECT LOCAL DATA ---
    print(f"💾 Local cache found for {ticker}. Inspecting data boundaries...")
    df_local = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df_local.index = pd.to_datetime(df_local.index).tz_localize(None) # Force format alignment
    df_local = df_local.sort_index()
    
    last_recorded_time = df_local.index.max()
    days_elapsed = (now.date() - last_recorded_time.date()).days

    print(f"⏳ Last cached data point: {last_recorded_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏳ Calendar days elapsed since last update: {days_elapsed} days")

    # Guard clause: If checked in the same hour, step away from the network entirely
    if days_elapsed == 0 and now.hour == last_recorded_time.hour:
        print("✅ Cache is structurally up to date for this hour. Skipping internet fetch entirely.")
        df_daily = df_local.resample('D').last().dropna()
        return df_daily, df_local

    # --- STEP 3: DYNAMIC OVERLAP BUFFER FETCH ---
    safe_days_to_fetch = max(5, days_elapsed + 3)
    print(f"🌐 Fetching overlapping buffer of last {safe_days_to_fetch} days from Yahoo...")
    
    try:
        df_delta = yf.download(ticker, period=f"{safe_days_to_fetch}d", interval="1h")
        
        if df_delta.empty:
            print("⚠️ Yahoo returned an empty set for this window (e.g. weekend/holiday). Falling back to cache.")
            df_daily = df_local.resample('D').last().dropna()
            return df_daily, df_local

        # --- STEP 4: RECONCILE AND DE-DUPLICATE VIA PANDAS ---
        # 1. Flatten delta columns if they are a MultiIndex
        if isinstance(df_delta.columns, pd.MultiIndex):
            df_delta.columns = df_delta.columns.get_level_values(0)

        # 2. Force identical string column structures to prevent structural alignment failures
        df_delta.columns = [str(col) for col in df_delta.columns]
        df_local.columns = [str(col) for col in df_local.columns]

        # 3. Force delta index to match the clean string-parsed format from the CSV file
        df_delta.index = pd.to_datetime(df_delta.index).tz_localize(None)

        # 4. Splice datasets together
        df_combined = pd.concat([df_local, df_delta], axis=0)
        
        # 5. De-duplicate using the datetime index
        df_combined = df_combined[~df_combined.index.duplicated(keep='last')]
        df_combined = df_combined.sort_index()
        
        # 6. Save to disk cleanly
        df_combined.index.name = "Datetime"
        df_combined.to_csv(cache_path)
        print(f"💾 Cache structurally updated and written to disk for {ticker}.")
        
        df_daily = df_combined.resample('D').last().dropna()
        return df_daily, df_combined
        
    except Exception as e:
        print(f"❌ Failed to update cache for {ticker}: {e}")
        # Always return a valid tuple fallback even on failures
        df_daily = df_local.resample('D').last().dropna()
        return df_daily, df_local

def generate_mock_signal_data(target_signal="BUY"):
    """
    Generates a deterministic 30-day dataset matching real yfinance hourly offsets.
    Includes slight random variance so standard deviation calculations do not equal 0.
    """
    import numpy as np
    base_dates = pd.date_range(start="2026-01-01", periods=10, freq="D") # Increased slightly for lookbacks
    timestamps = []
    
    market_hours = ["09:30:00", "10:30:00", "11:30:00", "12:30:00", "13:30:00", "14:30:00", "15:30:00"]
    
    for d in base_dates:
        if d.weekday() >= 5: 
            continue
        for hour_str in market_hours:
            timestamps.append(pd.Timestamp(f"{d.strftime('%Y-%m-%d')} {hour_str}"))
            
    df_hourly = pd.DataFrame(index=timestamps)
    
    # 🟢 CRITICAL: Add minor variance so Std Dev is never 0
    np.random.seed(42) # Keeps test data identical every run
    df_hourly['Adj Close'] = 100.0 + np.random.normal(0, 0.5, len(df_hourly))
    
    # Target the 14:30:00 bar on the final day
    target_time = df_hourly.index[-1].normalize() + pd.Timedelta(hours=14, minutes=30)
    
    if target_signal == "BUY":
        df_hourly.loc[target_time, 'Adj Close'] = 50.0
    elif target_signal == "SELL":
        df_hourly.loc[target_time, 'Adj Close'] = 150.0
        
    return df_hourly
    """
    Generates a deterministic 25-day dataset.
    The first 24 days are perfectly flat ($100), meaning SMA=100 and Std=0.
    The final hourly bar intentionally forces a massive shift to guarantee a Z-score trigger.
    """
    # Create 25 days of hourly timestamps (9:30 to 15:30)
    timestamps = pd.date_range(start="2026-01-01", periods=25 * 7, freq="h")
    
    # Initialize everything at a flat $100 base
    df_hourly = pd.DataFrame(index=timestamps)
    df_hourly['Adj Close'] = 100.0
    
    # Manipulate the very last row to force our target signal
    if target_signal == "BUY":
        # Force a massive drop on the final hour to trigger oversold (Z <= -2)
        df_hourly.iloc[-1, df_hourly.columns.get_loc('Adj Close')] = 50.0
    elif target_signal == "SELL":
        # Force a massive spike on the final hour to trigger overbought (Z >= 2)
        df_hourly.iloc[-1, df_hourly.columns.get_loc('Adj Close')] = 150.0
        
    return df_hourly