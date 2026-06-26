import pandas as pd


def run_backtest(df_hourly, df_daily_indicators, ticker,
                            mode="BACKTEST", target_hours=(9, 14),
                            take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28):
    trades = []
    active_trade = None

    df_hourly = df_hourly.copy()
    df_hourly['date_str'] = df_hourly.index.strftime('%Y-%m-%d')

    for i in range(len(df_hourly)):
        current_time = df_hourly.index[i]
        current_price = df_hourly['Close'].iloc[i]
        current_date_str = df_hourly['date_str'].iloc[i]

        if active_trade:
            active_trade['hours_held'] += 1
            price_change = (current_price - active_trade['Entry Price']) / active_trade['Entry Price']

            if price_change >= take_profit:
                active_trade.update({'Exit Price': current_price, 'Exit Time': current_time, 'Result': 'WIN', 'Return': price_change})
                trades.append(active_trade)
                active_trade = None
                continue

            elif price_change <= -stop_loss:
                active_trade.update({'Exit Price': current_price, 'Exit Time': current_time, 'Result': 'LOSS', 'Return': price_change})
                trades.append(active_trade)
                active_trade = None
                continue

            elif active_trade['hours_held'] >= max_hours_to_hold:
                active_trade.update({
                    'Exit Price': current_price, 'Exit Time': current_time, 'Return': price_change,
                    'Result': 'TWIN' if price_change > 0 else 'TLOSS'
                })
                trades.append(active_trade)
                active_trade = None
                continue

            continue

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
                    'Ticker': ticker,
                    'Entry Time': current_time,
                    'Entry Price': current_price,
                    'Exit Time': None,
                    'Exit Price': None,
                    'hours_held': 0,
                    'Result': 'OPEN',
                    'Return': 0.0
                }

    if active_trade:
        active_trade.update({
            'Exit Price': df_hourly['Close'].iloc[-1],
            'Exit Time': df_hourly.index[-1],
            'Result': 'OPEN',
            'Return': (df_hourly['Close'].iloc[-1] - active_trade['Entry Price']) / active_trade['Entry Price']
        })
        trades.append(active_trade)

    return trades
