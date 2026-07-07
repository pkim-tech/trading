import sqlite3
import streamlit as st
import pandas as pd

DB_PATH = "./cache/watchlist_sweep.db"

RESULT_COLORS = {
    'WIN':   '#2ecc71',
    'TWIN':  '#4c9be8',
    'LOSS':  '#e74c3c',
    'TLOSS': '#f0a500',
    'OPEN':  '#95a5a6',
}

st.set_page_config(layout="wide", page_title="Watchlist Trade Pivot (test)")
st.title("Watchlist Trade Pivot")
st.caption("Test page — reads a scoped snapshot (cache/watchlist_sweep.db), not the production DB. "
           "Trade-level results are computed once via the real kernel and cached in trade_cache, "
           "not read from backtest_cache's (sometimes stale) aggregate win_rate columns.")


@st.cache_data(ttl=300)
def load_nodes():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM watch_list ORDER BY ticker").fetchall()]


@st.cache_data(ttl=300)
def load_trades(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 z_score_threshold, fixed_sl, trail_buy_pct, trail_pct):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT entry_time, entry_price, exit_time, exit_price, hours_held, result, return_pct
            FROM trade_cache
            WHERE ticker=? AND strategy=? AND version=? AND window=? AND take_profit=? AND stop_loss=?
              AND max_hold_hours=? AND z_score_threshold=?
              AND COALESCE(fixed_sl,0)=? AND COALESCE(trail_buy_pct,0)=? AND COALESCE(trail_pct,0)=?
            ORDER BY entry_time
        """, (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
              z_score_threshold, fixed_sl or 0.0, trail_buy_pct or 0.0, trail_pct or 0.0)).fetchall()
        return [dict(r) for r in rows]


nodes = load_nodes()
if not nodes:
    st.warning("No nodes found in watchlist_sweep.db's watch_list table.")
    st.stop()

summary_rows = []
node_trades = {}
for node in nodes:
    trades = load_trades(node['ticker'], node['strategy'], node['version'], node['window'],
                          node['take_profit'], node['stop_loss'], node['max_hold_hours'],
                          node['z_score_threshold'], node['fixed_sl'], node['trail_buy_pct'],
                          node['trail_pct'])
    node_trades[node['id']] = trades
    counts = {k: 0 for k in RESULT_COLORS}
    for t in trades:
        counts[t['result']] = counts.get(t['result'], 0) + 1
    closed = sum(v for k, v in counts.items() if k != 'OPEN')
    win_twin = counts['WIN'] + counts['TWIN']
    compounded = 1.0
    for t in trades:
        if t['result'] != 'OPEN':
            compounded *= (1 + t['return_pct'])
    summary_rows.append({
        'Ticker': node['ticker'], 'Mode': node['mode'], 'Version': node['version'],
        'Trades': closed, 'WIN': counts['WIN'], 'LOSS': counts['LOSS'],
        'TWIN': counts['TWIN'], 'TLOSS': counts['TLOSS'], 'OPEN': counts['OPEN'],
        'Win+TWin %': round(100 * win_twin / closed, 1) if closed else None,
        'Compounded Return %': round((compounded - 1) * 100, 1),
    })

st.subheader("Watchlist summary")
st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

st.subheader("Drill into a node")
ticker_labels = {n['id']: f"{n['ticker']} ({n['version']}, {n['mode']})" for n in nodes}
selected_id = st.selectbox("Node", options=list(ticker_labels.keys()), format_func=lambda i: ticker_labels[i])
node = next(n for n in nodes if n['id'] == selected_id)
trades = node_trades[selected_id]

st.write(f"**{node['ticker']}** — {node['strategy']} {node['version']} — "
         f"window={node['window']} arm/tp={node['take_profit']}% trail_buy={node['trail_buy_pct']}% "
         f"trail_sell={node['trail_pct']}% max_hold={node['max_hold_hours']}h z={node['z_score_threshold']}")

df = pd.DataFrame(trades)
if not df.empty:
    df['return_pct'] = (df['return_pct'] * 100).round(2)
    st.dataframe(
        df.style.apply(lambda row: [f"background-color: {RESULT_COLORS.get(row['result'], '')}22"] * len(row), axis=1),
        use_container_width=True, hide_index=True,
    )
else:
    st.info("No cached trades for this node.")
