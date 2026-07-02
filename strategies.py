import pandas as pd


class BaseStrategy:
    def __init__(self, **kwargs):
        self.params = kwargs

    def generate_daily_indicators(self, df_daily):
        raise NotImplementedError

    def check_signal(self, current_price, prior_day_indicators):
        raise NotImplementedError

    def check_exit(self, current_price, entry_price, take_profit, stop_loss, hours_held, max_hold_hours):
        pct = (current_price - entry_price) / entry_price
        if pct >= take_profit / 100.0:
            return 'TP', entry_price * (1 + take_profit / 100.0)
        if pct <= -stop_loss / 100.0:
            return 'SL', entry_price * (1 - stop_loss / 100.0)
        if hours_held >= max_hold_hours:
            return 'TIME', current_price
        return None, None


class ZScoreBreakout(BaseStrategy):
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std = prior_day['SMA'], prior_day['Std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if current_price <= sma - std * threshold else 'HOLD'


class TrailingExitZScoreBreakout(BaseStrategy):
    """v1.8: close-based entry (v1.5 style), trailing stop exit once TP% is cleared."""
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std = prior_day['SMA'], prior_day['Std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if current_price <= sma - std * threshold else 'HOLD'


class LimitOrderZScoreBreakout(BaseStrategy):
    """v1.7: limit order entry at lower_band (fill on Low touch), intrabar stop loss."""
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std = prior_day['SMA'], prior_day['Std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if current_price <= sma - std * threshold else 'HOLD'


class TrendFilteredZScore(BaseStrategy):
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        df['Trend_Filter'] = df['Close'].rolling(window=50).mean()
        return df[['SMA', 'Std', 'Trend_Filter']].dropna()

    def check_signal(self, current_price, prior_day):
        sma, std, trend = prior_day['SMA'], prior_day['Std'], prior_day['Trend_Filter']
        if pd.isna(sma) or pd.isna(std) or pd.isna(trend):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if current_price <= sma - std * threshold and current_price > trend else 'HOLD'


