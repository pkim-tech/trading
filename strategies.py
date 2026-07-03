import pandas as pd


class BaseStrategy:
    def __init__(self, **kwargs):
        self.params = kwargs

    def generate_daily_indicators(self, df_daily):
        raise NotImplementedError

    def check_signal(self, ctx):
        raise NotImplementedError

    def check_exit(self, ctx):
        raise NotImplementedError


def _entry_price_field(ctx):
    """Close for bar-close entry strategies, Low for intrabar-touch entry strategies."""
    return ctx['current_price']


class ZScoreBreakout(BaseStrategy):
    """v1.5/v1.6: bar-close entry, bar-close TP/SL/TIME. Mirrors backtester._simulate."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        # backtester._simulate: TP, then SL, then TIME — bar-close only, every check.
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        cp, ep = ctx['current_price'], ctx['entry_price']
        pc = (cp - ep) / ep
        if pc >= ctx['take_profit']:
            return 'TP', cp, ctx.get('state', {})
        if pc <= -ctx['stop_loss']:
            return 'SL', cp, ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', cp, ctx.get('state', {})
        return None, None, ctx.get('state', {})


class TrailingExitZScoreBreakout(BaseStrategy):
    """v1.8: bar-close entry. SL + trailing-stop are intrabar (continuous);
    TP-activation and TIME (pre-activation) are bar-close. Mirrors backtester._simulate_trail."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        state = dict(ctx.get('state', {}))
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        trail_pct  = self.params.get('trail_pct', 0.03)

        if state.get('trailing'):
            peak = max(state.get('peak', ep), ctx['high'])
            state['peak'] = peak
            trail_stop = peak * (1 - trail_pct)
            if ctx['low'] <= trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']:
                exit_px = trail_stop if ctx['low'] <= trail_stop else ctx['current_price']
                reason = 'WIN' if exit_px > ep else 'LOSS'
                return reason, exit_px, state
            return None, None, state

        if ctx['low'] <= stop_price:
            return 'SL', stop_price, state

        if not ctx.get('at_bar_close', True):
            return None, None, state

        if ctx['current_price'] >= tp_price:
            state['trailing'] = True
            state['peak'] = ctx['current_price']
            return None, None, state

        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], state

        return None, None, state


class LimitOrderZScoreBreakout(BaseStrategy):
    """v1.7: intrabar-touch entry (Low vs band). SL is intrabar (continuous);
    TP and TIME are bar-close. Mirrors backtester._simulate_limit."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        # Entry uses Low (intrabar touch), not Close — differs from the other strategies.
        return 'BUY' if ctx['low'] <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        # Note: for v1.7, entry_price passed in is the fill price (lower_band at entry),
        # not necessarily the live price at signal time.
        ep = ctx['entry_price']
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])

        if ctx['low'] <= stop_price:
            return 'SL', stop_price, ctx.get('state', {})

        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})

        if ctx['current_price'] >= tp_price:
            return 'TP', ctx['current_price'], ctx.get('state', {})

        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], ctx.get('state', {})

        return None, None, ctx.get('state', {})


class TrailingBuyZScoreBreakout(BaseStrategy):
    """v1.9: after z-score signal, waits for price to bounce trail_buy_pct% above running low before entering.
    Exit: fixed TP (bar-close) + fixed SL (intrabar) + hold cap. Mirrors backtester._simulate_trail_buy."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        if ctx['low'] <= stop_price:
            return 'SL', stop_price, ctx.get('state', {})
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        if ctx['current_price'] >= tp_price:
            return 'TP', ctx['current_price'], ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], ctx.get('state', {})
        return None, None, ctx.get('state', {})


class TrailingBothZScoreBreakout(TrailingBuyZScoreBreakout):
    """v1.10: trailing entry (bounce above running low) + trailing exit once TP% cleared.
    Mirrors backtester._simulate_trail_both."""

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        state = dict(ctx.get('state', {}))
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        trail_pct  = self.params.get('trail_pct', 0.03)

        if state.get('trailing'):
            peak = max(state.get('peak', ep), ctx['high'])
            state['peak'] = peak
            trail_stop = peak * (1 - trail_pct)
            if ctx['low'] <= trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']:
                exit_px = trail_stop if ctx['low'] <= trail_stop else ctx['current_price']
                reason = 'WIN' if exit_px > ep else 'LOSS'
                return reason, exit_px, state
            return None, None, state

        if ctx['low'] <= stop_price:
            return 'SL', stop_price, state
        if not ctx.get('at_bar_close', True):
            return None, None, state
        if ctx['current_price'] >= tp_price:
            state['trailing'] = True
            state['peak'] = ctx['current_price']
            return None, None, state
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], state
        return None, None, state


class TrendFilteredZScore(BaseStrategy):
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        df['Trend_Filter'] = df['Close'].rolling(window=50).mean()
        return df[['SMA', 'Std', 'Trend_Filter']].dropna()

    def check_signal(self, ctx):
        sma, std, trend = ctx['sma'], ctx['std'], ctx['trend']
        if pd.isna(sma) or pd.isna(std) or pd.isna(trend):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        cp = _entry_price_field(ctx)
        return 'BUY' if cp <= sma - std * threshold and cp > trend else 'HOLD'

    def check_exit(self, ctx):
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        cp, ep = ctx['current_price'], ctx['entry_price']
        pc = (cp - ep) / ep
        if pc >= ctx['take_profit']:
            return 'TP', cp, ctx.get('state', {})
        if pc <= -ctx['stop_loss']:
            return 'SL', cp, ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', cp, ctx.get('state', {})
        return None, None, ctx.get('state', {})
