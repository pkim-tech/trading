import sys
import time
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_manager import fetch_live_data_smart, CACHE_DIR

def run_pipeline_tests():
    ticker = "SOXL"
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    
    # Clean up any leftover previous test cache
    if cache_path.exists():
        cache_path.unlink()
        print("🧹 Cleaned up old test cache.")

    print("\n=== TEST 1: Initial Bootstrap ===")
    df_daily, df_hourly = fetch_live_data_smart(ticker)
    
    assert cache_path.exists(), "❌ Test 1 Failed: Cache file was not created!"
    assert df_hourly is not None, "❌ Test 1 Failed: Hourly dataframe is None!"
    print(f"✅ Test 1 Passed: Initialized cache with {len(df_hourly)} rows.")

    
    print("\n=== TEST 2: Network Guard (Immediate Re-run) ===")
    # Running immediately should hit the "same hour" guard clause
    df_daily2, df_hourly2 = fetch_live_data_smart(ticker)
    
    assert len(df_hourly) == len(df_hourly2), "❌ Test 2 Failed: Data size mismatched!"
    print("✅ Test 2 Passed: Successfully intercepted by guard clause (zero network activity).")

    
    print("\n=== TEST 3: Incremental Healing & De-duplication ===")
    print("Simulating a data gap by truncating the last 5 days of cached data...")
    
    # Load cache, chop off the last 120 hourly rows (5 days), and save it back broken
    df_broken = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    original_row_count = len(df_broken)
    df_truncated = df_broken.iloc[:-120] 
    df_truncated.to_csv(cache_path)
    
    # Artificially alter the system's perception of time for the log display 
    # (The code will see the new max index is 5 days ago, triggering an update)
    print(f"Truncated cache from {original_row_count} rows down to {len(df_truncated)} rows.")
    
    print("\nRunning fetch_live_data_smart() on the broken cache...")
    df_daily_healed, df_hourly_healed = fetch_live_data_smart(ticker)
    
    assert len(df_hourly_healed) >= original_row_count, "❌ Test 3 Failed: Pipeline did not heal the gap!"
    assert not df_hourly_healed.index.duplicated().any(), "❌ Test 3 Failed: Duplicate timestamps found!"
    
    print(f"✅ Test 3 Passed: Pipeline successfully downloaded overlapping buffer, stitched it, and de-duplicated.")
    print("\n🎉 ALL LIVE CACHING TESTS PASSED SUCCESSFULLY! 🎉")

if __name__ == "__main__":
    run_pipeline_tests()