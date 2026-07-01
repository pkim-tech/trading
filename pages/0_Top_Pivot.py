import sqlite3
import streamlit as st
import pandas as pd
from db_cache import get_kv

DB_PATH = "./cache/trading_universe.db"
WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]

st.set_page_config(layout="wide", page_title="Top Pivot")
st.title("Top Pivot")


@st.cache_data(ttl=3600)
def load_versions():
    v = get_kv("versions")
    if v is None:
        with sqlite3.connect(DB_PATH) as c:
            v = [r[0] for r in c.execute(
                "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
            ).fetchall()]
    return v


@st.cache_data(ttl=3600)
def load_single_stock_tickers():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT symbol FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''"
        ).fetchall()
    return {r[0] for r in rows}


@st.cache_data(ttl=3600)
def load_index_tickers():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT symbol FROM tickers WHERE index_underlier IS NOT NULL AND index_underlier != ''"
        ).fetchall()
    return {r[0] for r in rows}


def _build_pivot(df_cells, df_meta, min_trades):
    df_filtered = df_cells[df_cells['trades'] >= min_trades]
    if df_filtered.empty:
        return pd.DataFrame()
    df_agg = df_filtered.groupby(['ticker', 'window', 'z'])['strategy_return'].max().reset_index()
    pivot = df_agg.pivot_table(index='ticker', columns=['window', 'z'], values='strategy_return')
    pivot.columns = [f"w{int(w)} z{z}" for w, z in pivot.columns]
    col_order = [f"w{w} z{z}" for w in WINDOWS for z in Z_THRESHOLDS]
    pivot = pivot[[c for c in col_order if c in pivot.columns]]
    pivot['max'] = pivot.max(axis=1)
    pivot = pivot.join(df_meta.set_index('ticker'), how='left')
    return pivot.sort_values('max', ascending=False)


@st.cache_data(ttl=3600)
def load_pivot_from_cache(version):
    cells = get_kv(f"pivot_cells_{version}")
    meta = get_kv(f"pivot_meta_{version}")
    if cells is None or meta is None:
        return None, None
    return pd.DataFrame(cells), pd.DataFrame(meta)


@st.cache_data(ttl=86400)
def load_pivot_from_db(version, min_trades):
    with sqlite3.connect(DB_PATH) as c:
        df_cells = pd.read_sql_query(
            """SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                      trades, MAX(strategy_return) AS strategy_return
               FROM backtest_cache
               WHERE version = ? AND window IN (10, 20)
               GROUP BY ticker, window, z_score_threshold, trades""",
            c, params=(version,)
        )
        df_meta = pd.read_sql_query(
            """WITH best AS (
                   SELECT ticker, strategy_return, alpha_vs_spy, asset_bh,
                          CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh ELSE NULL END AS bh_mult,
                          ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY strategy_return DESC) AS rn
                   FROM backtest_cache
                   WHERE version = ? AND window IN (10, 20)
               )
               SELECT ticker, alpha_vs_spy, asset_bh, bh_mult FROM best WHERE rn = 1""",
            c, params=(version,)
        )
    return df_cells, df_meta


def load_pivot(version, min_trades):
    df_cells, df_meta = load_pivot_from_cache(version)
    if df_cells is None:
        df_cells, df_meta = load_pivot_from_db(version, min_trades)
    return _build_pivot(df_cells, df_meta, min_trades)


versions = load_versions()
if not versions:
    st.info("No backtest results yet.")
    st.stop()

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    version = st.selectbox("Version", versions)
with c2:
    min_trades = st.number_input("Min trades", min_value=1, value=5, step=1)
with c3:
    min_return = st.number_input("Min max return %", value=100.0, step=50.0, format="%.0f")
with c4:
    min_alpha = st.number_input("Min alpha %", value=0.0, step=10.0, format="%.0f")
with c5:
    min_bh_mult = st.number_input("Min B&H mult", value=1.0, step=0.5, format="%.1f")
with c6:
    exclude_single_stock = st.toggle("Exclude single-stock", value=True)
    exclude_index = st.toggle("Exclude index", value=False)

pivot = load_pivot(version, int(min_trades))
if pivot.empty:
    st.info("No data.")
    st.stop()

single_stock = load_single_stock_tickers()
index_tickers = load_index_tickers()

if exclude_single_stock:
    pivot = pivot[~pivot.index.isin(single_stock)]
if exclude_index:
    pivot = pivot[~pivot.index.isin(index_tickers)]

pivot = pivot[
    (pivot['max'] >= min_return) &
    (pivot['alpha_vs_spy'] >= min_alpha) &
    (pivot['bh_mult'] >= min_bh_mult)
]

if pivot.empty:
    st.info("No tickers match the current filters.")
    st.stop()

st.caption(f"{len(pivot)} tickers — click a cell to open in Node Inspector")

@st.cache_data(ttl=3600)
def load_underlier_map():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT symbol, stock_underlier FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''").fetchall()
    return {r[0]: r[1] for r in rows}

@st.cache_data(ttl=86400)
def load_best_nodes(version):
    cached = get_kv(f"best_nodes_{version}")
    if cached:
        return {(k.split("|")[0], int(k.split("|")[1]), float(k.split("|")[2])): tuple(v)
                for k, v in cached.items()}
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("""
            WITH best AS (
                SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                       take_profit, stop_loss, max_hold_hours,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker, window, COALESCE(z_score_threshold, 2.0)
                           ORDER BY alpha_vs_spy DESC
                       ) AS rn
                FROM backtest_cache WHERE version = ?
            )
            SELECT ticker, window, z, take_profit, stop_loss, max_hold_hours
            FROM best WHERE rn = 1
        """, (version,)).fetchall()
    return {(r[0], int(r[1]), float(r[2])): (int(r[3]), int(r[4]), int(r[5])) for r in rows}

underlier_map = load_underlier_map()
best_nodes = load_best_nodes(version)
wz_cols = [c for c in pivot.columns if c.startswith("w") and " z" in c]

def build_pivot_html(pivot, best_nodes, version, wz_cols, underlier_map):
    th_base = 'padding:5px 10px; font-size:12px; color:#aaa; border-bottom:1px solid #333; white-space:nowrap; cursor:pointer; user-select:none;'
    td  = 'style="padding:4px 10px; text-align:right; font-size:13px; border-bottom:1px solid #1e1e1e;"'
    td_l = 'style="padding:4px 10px; text-align:left; font-size:13px; border-bottom:1px solid #1e1e1e; white-space:nowrap;"'

    other_cols = [c for c in pivot.columns if c not in wz_cols]
    ordered_cols = wz_cols + other_cols
    all_cols = ['Ticker', 'Underlier'] + ordered_cols

    def th_tag(label, idx, align='right'):
        return f'<th onclick="sortTable({idx})" style="{th_base} text-align:{align};">{label} <span style="color:#555;">⇅</span></th>'

    headers = [th_tag('Ticker', 0, 'left'), th_tag('Underlier', 1, 'left')]
    for i, col in enumerate(ordered_cols, start=2):
        headers.append(th_tag(col, i))

    rows = []
    for ticker in pivot.index:
        underlier = underlier_map.get(ticker, '')
        cells = [f'<td {td_l}><b>{ticker}</b></td>', f'<td {td_l} style="color:#666;">{underlier}</td>']
        for col in ordered_cols:
            val = pivot.loc[ticker, col]
            if pd.isna(val):
                cells.append(f'<td {td} data-val="-999">—</td>')
            elif col in wz_cols:
                w_str, z_str = col[1:].split(' z')
                node = best_nodes.get((ticker, int(w_str), float(z_str)))
                if node:
                    tp, sl, hold = node
                    url = f"/Node_Inspector?ticker={ticker}&version={version}&window={w_str}&z={z_str}&take_profit={tp}&stop_loss={sl}&max_hold_hours={hold}"
                    cells.append(f'<td {td} data-val="{val:.2f}"><a href="{url}" target="_top" style="color:#4ade80; text-decoration:none; font-weight:500;">{val:.0f}%</a></td>')
                else:
                    cells.append(f'<td {td} data-val="{val:.2f}">{val:.0f}%</td>')
            elif col == 'bh_mult':
                cells.append(f'<td {td} data-val="{val:.4f}">{val:.1f}x</td>')
            else:
                cells.append(f'<td {td} data-val="{val:.2f}">{val:.0f}%</td>')
        rows.append(f'<tr>{"".join(cells)}</tr>')

    script = """
<script>
var _sortDir = {};
function sortTable(col) {
    var tbl = document.getElementById('pivot');
    var tbody = tbl.tBodies[0];
    var rows = Array.from(tbody.rows);
    var asc = !_sortDir[col];
    _sortDir = {};
    _sortDir[col] = asc;
    rows.sort(function(a, b) {
        var av = a.cells[col].dataset.val !== undefined ? parseFloat(a.cells[col].dataset.val) : a.cells[col].innerText;
        var bv = b.cells[col].dataset.val !== undefined ? parseFloat(b.cells[col].dataset.val) : b.cells[col].innerText;
        if (isNaN(av)) av = a.cells[col].innerText;
        if (isNaN(bv)) bv = b.cells[col].innerText;
        return asc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
    });
    rows.forEach(function(r) { tbody.appendChild(r); });
}
</script>"""

    return f'''{script}<div style="overflow-x:auto;"><table id="pivot" style="width:100%; border-collapse:collapse; background:#0e1117;">
        <thead><tr>{"".join(headers)}</tr></thead>
        <tbody>{"".join(rows)}</tbody>
    </table></div>'''

st.html(build_pivot_html(pivot, best_nodes, version, wz_cols, underlier_map))

with st.expander("Edit Underliers"):
    display = pivot.copy()
    display.insert(0, 'Underlier', display.index.map(underlier_map).fillna(''))
    pct_cols = [c for c in pivot.columns if c not in ('bh_mult',)]
    col_config = {
        'Underlier': st.column_config.TextColumn("Underlier"),
        **{col: st.column_config.NumberColumn(format="%.0f%%") for col in pct_cols},
        'bh_mult': st.column_config.NumberColumn("B&H Mult", format="%.1fx"),
    }
    edited = st.data_editor(display, use_container_width=True, hide_index=False,
                            column_config=col_config,
                            disabled=[c for c in display.columns if c != 'Underlier'])
    changed = display['Underlier'] != edited['Underlier']
    if changed.any():
        with sqlite3.connect(DB_PATH) as conn:
            for ticker in display.index[changed]:
                val = edited.loc[ticker, 'Underlier'].strip() or None
                conn.execute("UPDATE tickers SET stock_underlier = ? WHERE symbol = ?", (val, ticker))
        load_single_stock_tickers.clear()
        load_underlier_map.clear()
        st.rerun()
