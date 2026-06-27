import json
import sqlite3
import streamlit as st
import pandas as pd
from pathlib import Path
from active_signals import add_node, remove_node, get_watchlist, label_node

DB_PATH      = "./cache/trading_universe.db"
DISMISS_FILE = Path("./cache/dismissed_tickers.json")


def load_dismissed() -> set:
    # stored as list of [ticker, strategy] pairs
    if DISMISS_FILE.exists():
        return {tuple(x) for x in json.loads(DISMISS_FILE.read_text())}
    return set()


def save_dismissed(dismissed: set):
    DISMISS_FILE.write_text(json.dumps(sorted(list(dismissed))))

st.set_page_config(layout="wide", page_title="Winners")
st.title("Winners")


@st.cache_data(ttl=60)
def load_results(version):
    with sqlite3.connect(DB_PATH) as c:
        return pd.read_sql_query(
            """SELECT ticker, strategy, window, take_profit, stop_loss,
                      max_hold_hours, trades, win_rate, strategy_return, alpha_vs_spy,
                      asset_bh, spy_bh
               FROM backtest_cache
               WHERE version = ?
               ORDER BY alpha_vs_spy DESC""",
            c, params=(version,)
        )


@st.cache_data(ttl=300)
def latest_ticker_stats(ticker: str) -> dict:
    path = Path(f"./cache/{ticker}_1h.csv")
    if not path.exists():
        return {}
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.empty:
        return {}
    df.index = pd.to_datetime(df.index).tz_localize(None)
    last_day = df.index.normalize().max()
    vol = int(df[df.index.normalize() == last_day]['Volume'].sum()) if 'Volume' in df.columns else None
    price = float(df['Close'].dropna().iloc[-1]) if 'Close' in df.columns else None
    return {'vol': vol, 'price': price}


@st.cache_data(ttl=60)
def load_versions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()
    return [r[0] for r in rows]


def watchlist_keys(version):
    return {
        (w['ticker'], w['strategy'], w['version'], w['window'],
         w['take_profit'], w['stop_loss'], w['max_hold_hours'])
        for w in get_watchlist()
        if w['version'] == version
    }


versions = load_versions()
if not versions:
    st.info("No backtest results found. Run a sweep first.")
    st.stop()

# --- Filters ---
dismissed = load_dismissed()

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    version = st.selectbox("Version", versions)
with c2:
    df_all = load_results(version)
    tickers_all = sorted(df_all['ticker'].unique())
    ticker_filter = st.multiselect("Ticker", tickers_all, default=tickers_all)
with c3:
    strategy_opts = sorted(df_all['strategy'].unique())
    strategy_filter = st.multiselect("Strategy", strategy_opts, default=strategy_opts)
with c4:
    min_trades = st.number_input("Min trades", min_value=0, value=5, step=1)
with c5:
    min_alpha = st.number_input("Min alpha %", value=0.0, step=1.0, format="%.1f")
with c6:
    top_n = st.number_input("Top N per ticker", min_value=1, value=5, step=1)

c7, c8, c9 = st.columns(3)
with c7:
    min_return = st.number_input("Min return %", value=100.0, step=10.0, format="%.0f")
with c8:
    min_bh_mult = st.number_input("Min B&H multiplier", value=2.0, step=0.5, format="%.1f")
with c9:
    beat_bh = st.toggle("Beat asset B&H", value=True)
show_dismissed = st.toggle("Show dismissed", value=False)
if dismissed and show_dismissed:
    st.caption(f"Dismissed: {', '.join(f'{t}/{s}' for t, s, v in sorted(dismissed) if v == version)}")

df_all['bh_mult'] = df_all.apply(
    lambda r: r['strategy_return'] / r['asset_bh'] if r['asset_bh'] > 0 else None, axis=1
)

is_dismissed = df_all.apply(lambda r: (r['ticker'], r['strategy'], version) in dismissed, axis=1)
df = df_all[
    df_all['ticker'].isin(ticker_filter) &
    df_all['strategy'].isin(strategy_filter) &
    (df_all['trades'] >= min_trades) &
    (df_all['alpha_vs_spy'] >= min_alpha) &
    (df_all['strategy_return'] >= min_return) &
    (df_all['bh_mult'].fillna(0) >= min_bh_mult) &
    (~beat_bh | (df_all['strategy_return'] > df_all['asset_bh'])) &
    (show_dismissed | ~is_dismissed)
]

df = (
    df.sort_values('alpha_vs_spy', ascending=False)
      .groupby('ticker', sort=False)
      .head(int(top_n))
      .reset_index(drop=True)
)

if df.empty:
    st.info("No nodes match the current filters.")
    st.stop()

st.caption(f"{len(df)} nodes  ·  {df['ticker'].nunique()} tickers")

watched = watchlist_keys(version)

display = df.copy()
_stats = display['ticker'].map(latest_ticker_stats)
display['vol']   = _stats.map(lambda s: s.get('vol'))
display['price'] = _stats.map(lambda s: s.get('price'))
display['win_rate']        = display['win_rate'].map(lambda x: f"{x:.0f}%")
display['strategy_return'] = display['strategy_return'].map(lambda x: f"{x:.1f}%")
display['alpha_vs_spy']    = display['alpha_vs_spy'].map(lambda x: f"{x:.1f}%")
display['asset_bh']        = display['asset_bh'].map(lambda x: f"{x:.1f}%")
display['spy_bh']          = display['spy_bh'].map(lambda x: f"{x:.1f}%")
display['bh_mult']         = display['bh_mult'].map(lambda x: f"{x:.1f}x" if pd.notna(x) else "")
display = display.rename(columns={
    'ticker': 'Ticker', 'strategy': 'Strategy', 'window': 'Win',
    'take_profit': 'TP%', 'stop_loss': 'SL%', 'max_hold_hours': 'Hold h',
    'trades': 'Trades', 'win_rate': 'Win%', 'strategy_return': 'Return',
    'alpha_vs_spy': 'Alpha', 'asset_bh': 'Asset B&H', 'spy_bh': 'SPY B&H',
    'bh_mult': 'B&H Mult', 'vol': 'Vol (last day)', 'price': 'Last Price',
})

selection = st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
)

selected_rows = selection.selection.rows
if selected_rows:
    i   = selected_rows[0]
    r   = df.loc[i]
    key = (r['ticker'], r['strategy'], version, int(r['window']),
           int(r['take_profit']), int(r['stop_loss']), int(r['max_hold_hours']))
    is_watched   = key in watched
    is_dismissed = (r['ticker'], r['strategy'], version) in dismissed

    st.caption(
        f"**{r['ticker']}**  {r['strategy']}  "
        f"w={r['window']}  TP={r['take_profit']}%  SL={r['stop_loss']}%  hold={r['max_hold_hours']}h"
    )
    a1, a2, a3 = st.columns(3)

    with a1:
        watch_val = st.checkbox("Watch", value=is_watched, key=f"watch_{i}")
        if watch_val and not is_watched:
            add_node(r['ticker'], r['strategy'], version, int(r['window']),
                     int(r['take_profit']), int(r['stop_loss']), int(r['max_hold_hours']))
            st.cache_data.clear()
            st.rerun()
        elif not watch_val and is_watched:
            wl_by_key = {
                (w['ticker'], w['strategy'], w['version'], w['window'],
                 w['take_profit'], w['stop_loss'], w['max_hold_hours']): w['id']
                for w in get_watchlist()
            }
            if key in wl_by_key:
                remove_node(wl_by_key[key])
                st.cache_data.clear()
                st.rerun()

    with a2:
        dismiss_val = st.checkbox("Dismiss", value=is_dismissed, key=f"dismiss_{i}")
        if dismiss_val and not is_dismissed:
            dismissed.add((r['ticker'], r['strategy'], version))
            save_dismissed(dismissed)
            st.rerun()
        elif not dismiss_val and is_dismissed:
            dismissed.discard((r['ticker'], r['strategy'], version))
            save_dismissed(dismissed)
            st.rerun()

    with a3:
        if st.button("Open in Node Inspector"):
            st.session_state["target_node"] = {
                "ticker":        r['ticker'],
                "strategy":      r['strategy'],
                "version":       version,
                "window":        int(r['window']),
                "take_profit":   int(r['take_profit']),
                "stop_loss":     int(r['stop_loss']),
                "max_hold_hours": int(r['max_hold_hours']),
            }
            st.switch_page("pages/2_Node_Inspector.py")

st.divider()
st.subheader("Watch list")
wl = get_watchlist()
if wl:
    wl_df = pd.DataFrame(wl)

    # Join backtest stats
    with sqlite3.connect(DB_PATH) as c:
        stats = pd.read_sql_query(
            """SELECT ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                      trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh
               FROM backtest_cache""", c
        )
    wl_df = wl_df.merge(
        stats,
        on=['ticker', 'strategy', 'version', 'window', 'take_profit', 'stop_loss', 'max_hold_hours'],
        how='left',
    )

    wl_df['win_rate']        = wl_df['win_rate'].map(lambda x: f"{x:.0f}%" if pd.notna(x) else "")
    wl_df['strategy_return'] = wl_df['strategy_return'].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    wl_df['alpha_vs_spy']    = wl_df['alpha_vs_spy'].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    wl_df['asset_bh']        = wl_df['asset_bh'].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    wl_df['spy_bh']          = wl_df['spy_bh'].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "")

    wl_df['watch'] = True
    wl_display = wl_df[['id', 'ticker', 'strategy', 'version', 'window', 'take_profit',
                          'stop_loss', 'max_hold_hours', 'trades', 'win_rate',
                          'strategy_return', 'alpha_vs_spy', 'asset_bh', 'spy_bh', 'label', 'watch']].rename(columns={
        'id': 'ID', 'ticker': 'Ticker', 'strategy': 'Strategy', 'version': 'Version',
        'window': 'Win', 'take_profit': 'TP%', 'stop_loss': 'SL%', 'max_hold_hours': 'Hold h',
        'trades': 'Trades', 'win_rate': 'Win%', 'strategy_return': 'Return',
        'alpha_vs_spy': 'Alpha', 'asset_bh': 'Asset B&H', 'spy_bh': 'SPY B&H',
        'label': 'Label', 'watch': 'Watch',
    })

    wl_edited = st.data_editor(
        wl_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Label": st.column_config.TextColumn("Label"),
            "Watch": st.column_config.CheckboxColumn("Watch", help="Uncheck to remove"),
        },
        disabled=[c for c in wl_display.columns if c not in ('Label', 'Watch')],
    )

    # Remove unchecked rows
    for i in wl_display.index[wl_edited['Watch'] == False]:
        remove_node(int(wl_display.loc[i, 'ID']))
        st.cache_data.clear()
        st.rerun()

    # Save edited labels
    changed = wl_display['Label'] != wl_edited['Label']
    for i in wl_display.index[changed]:
        label_node(int(wl_display.loc[i, 'ID']), wl_edited.loc[i, 'Label'])
else:
    st.caption("Watch list is empty.")
