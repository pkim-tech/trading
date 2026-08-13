import logging
import os
import time
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime

from signals_helpers import detect_price_discontinuity, fix_one_bar_split_artifacts, get_real_splits
import db_cache

# Create a local directory named 'cache' to store data files
CACHE_DIR = Path("./cache/research")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _apply_split_artifact_fix(ticker, df):
    """Shared by both the bootstrap (Step 1) and incremental (Step 3) paths
    -- see signals_helpers.fix_one_bar_split_artifacts's docstring for the
    full design/history. Fetches the real split record fresh each call
    (cheap relative to the yf.download this always runs alongside); an empty
    result (fetch failure or genuinely no splits on file) means zero
    corrections are applied, never a fallback to the price-heuristic alone."""
    splits = get_real_splits(ticker)
    pre_fix_df = df
    df, fixes = fix_one_bar_split_artifacts(df, splits)
    for fix in fixes:
        print(f"⚠️ Corrected one-bar split artifact for {ticker} at {fix['ts']} "
              f"(factor={fix['factor']:.2f}, Close {fix['old_close']:.4f} -> {fix['new_close']:.4f})")
        try:
            db_cache.log_data_mutation(
                ticker, float(fix['factor']), str(fix['ts']), fix['old_close'], fix['new_close'],
                "one-bar split-artifact fix (real-split-confirmed)", pre_fix_df,
            )
        except Exception as e:
            print(f"⚠️ Failed to log data mutation for {ticker} (proceeding anyway): {e}")
    return df

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

            # Same-pull split-artifact guard (2026-08-13): the incremental-
            # fetch split-guard below only fires on a stale-cache-vs-fresh-
            # fetch mismatch, so it never covers this initial bootstrap pull
            # -- found live via 11 of 12 scripts/scan_bad_ticks.py "bad tick"
            # hits, all real splits whose split-effective bar yfinance served
            # partially-adjusted within this single 730-day pull. Gated on a
            # REAL confirmed split (get_real_splits) -- see
            # signals_helpers.fix_one_bar_split_artifacts's docstring for why
            # the price-heuristic-only version (an earlier draft of this fix)
            # was rejected by paired Opus review. Also applied in Step 3
            # below (df_combined), since a NEW split on an already-cached
            # ticker hits this same artifact shape and this bootstrap branch
            # only ever runs once per ticker.
            df_new = _apply_split_artifact_fix(ticker, df_new)

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

        # 3b. Corporate-action guard: yf.download() actually defaults to auto_adjust=True
        # (confirmed 2026-07-16/22 -- this comment previously claimed the opposite), but
        # that only adjusts the window being fetched *right now* for corporate actions known
        # as of today -- it doesn't reach back and re-adjust rows already sitting in the
        # local cache from a prior fetch. So a new split still shows up as every overlapping-
        # date local row being a near-identical multiple of the fresh delta row for the same
        # timestamp (the old cache is stale-scale, the new fetch is new-scale) -- ordinary
        # price volatility doesn't produce a *consistent* ratio across every overlap bar, and
        # magnitude alone can't tell a real crash from a split (a 3x leveraged ETF can
        # plausibly fall >66% in one real extreme day) -- match against known round-number
        # split factors instead, same logic as signals_compute.py's live-price check.
        # Rescale the whole local cache before merging so history stays on the same scale
        # as fresh data, instead of leaving a silent price cliff (found live 2026-07-15,
        # KORU's ~20:1 split -- see docs/research_log.md's 2026-07-22 entry for the full
        # auto_adjust/split-guard reconciliation).
        overlap = df_local.index.intersection(df_delta.index)
        if len(overlap) >= 1:
            ratios = df_local.loc[overlap, "Close"] / df_delta.loc[overlap, "Close"]
            consistent = len(overlap) == 1 or (ratios.std() / ratios.mean()) < 0.05
            split_ratio = detect_price_discontinuity(current_price=1.0, reference_price=ratios.mean())
            if consistent and split_ratio is not None:
                factor = ratios.mean()
                print(f"⚠️ Detected likely stock split for {ticker} (ratio={factor:.2f}) -- rescaling cached history.")
                overlap_bar_time = overlap[0]
                price_before = float(df_local.loc[overlap_bar_time, "Close"])
                try:
                    db_cache.log_data_mutation(
                        ticker, float(factor), str(overlap_bar_time), price_before,
                        price_before / factor,
                        f"split-guard rescale, {len(overlap)} overlap bar(s), consistent={consistent}",
                        df_local,
                    )
                except Exception as e:
                    print(f"⚠️ Failed to log data mutation for {ticker} (proceeding with rescale anyway): {e}")
                df_local[["Open", "High", "Low", "Close"]] = df_local[["Open", "High", "Low", "Close"]] / factor
                if "Volume" in df_local.columns:
                    df_local["Volume"] = df_local["Volume"] * factor

        # 4. Splice datasets together
        df_combined = pd.concat([df_local, df_delta], axis=0)
        
        # 5. De-duplicate using the datetime index
        df_combined = df_combined[~df_combined.index.duplicated(keep='last')]
        df_combined = df_combined.sort_index()

        # 5b. Revision-diff log (2026-08-12): the JNUG incident that motivated this had
        # zero trail anywhere -- data_collector.log only ever recorded "synced OK", never
        # whether the sync appended new bars, silently revised existing ones (keep='last'
        # above means a fresh Yahoo value always wins over what was already cached), or
        # both. This doesn't prevent a revision (Yahoo's data is still trusted as-is,
        # same as before) -- it just makes one detectable after the fact instead of
        # invisible. Uses logging (not print, unlike the rest of this function) so it
        # actually lands in data_collector.log via the handlers data_collector.py installs
        # on the root logger at import time -- the existing print() calls in this function
        # don't reach that file at all, confirmed empirically (2026-08-12: grepped for
        # this function's own print() text in the real log, zero hits).
        try:
            new_bars = df_delta.index.difference(df_local.index)
            price_cols = [c for c in ("Open", "High", "Low", "Close") if c in df_local.columns and c in df_delta.columns]
            revised = []
            for ts in overlap:
                old_row, new_row = df_local.loc[ts], df_delta.loc[ts]
                if any(abs(float(old_row[c]) - float(new_row[c])) > 1e-6 * max(1.0, abs(float(old_row[c]))) for c in price_cols):
                    revised.append((ts, float(old_row.get("Close", float("nan"))), float(new_row.get("Close", float("nan")))))
            logging.info(f"{ticker}: sync appended {len(new_bars)} new bar(s), "
                         f"revised {len(revised)} existing bar(s) out of {len(overlap)} overlapping")
            if revised:
                sample = ", ".join(f"{ts} Close {old:.4f}->{new:.4f}" for ts, old, new in revised[:5])
                logging.warning(f"{ticker}: {len(revised)} existing bar(s) silently revised on sync "
                                 f"(not a detected split -- see split-guard log above if this ticker also "
                                 f"triggered that): {sample}{' ...' if len(revised) > 5 else ''}")
        except Exception as e:
            logging.warning(f"{ticker}: revision-diff logging failed (proceeding with write anyway): {e}")

        # 5c. Same-pull split-artifact guard (2026-08-13) -- see
        # _apply_split_artifact_fix's docstring. Applied here (not just at
        # bootstrap) since a NEW split on an already-cached ticker produces
        # the identical single-bar artifact shape, and this incremental path
        # is what every subsequent update after bootstrap actually runs
        # through -- the far more common case going forward.
        df_combined = _apply_split_artifact_fix(ticker, df_combined)

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