import pandas as pd

class BaseStrategy:
    """Abstract base class to keep all future strategies uniform."""
    def __init__(self, **kwargs):
        self.params = kwargs
        
    def generate_daily_indicators(self, df_daily):
        raise NotImplementedError
        
    def check_signal(self, current_price, prior_day_indicators):
        raise NotImplementedError

class ZScoreBreakout(BaseStrategy):
    """Your Original Strategy: Buys when price drops below a lower Bollinger Band."""
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std = prior_day['SMA'], prior_day['Std']
        if std == 0 or pd.isna(sma) or pd.isna(std): 
            return "HOLD"
        
        # Original logic: entry if price is standard deviations below mean
        lower_threshold = sma - (std * 2.0)
        return "BUY" if current_price <= lower_threshold else "HOLD"

class TrendFilteredZScore(BaseStrategy):
    """Your NEW Strategy Twist: Only buys if the asset is in a macro uptrend

    (e.g., price is above a longer-term trend window).
    """
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        # Add a 50-day structural filter trend line
        df['Trend_Filter'] = df['Close'].rolling(window=50).mean()
        return df[['SMA', 'Std', 'Trend_Filter']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std, trend = prior_day['SMA'], prior_day['Std'], prior_day['Trend_Filter']
        if pd.isna(sma) or pd.isna(std) or pd.isna(trend): 
            return "HOLD"
        
        lower_threshold = sma - (std * 2.0)
        # Twist: Cut out whiplash by demanding the macro trend is pointing up!
        if current_price <= lower_threshold and current_price > trend:
            return "BUY"
        return "HOLD"