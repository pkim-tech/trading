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

st.divider()
st.subheader("Universe — Best Alpha by Strategy")

STRAT_SHORT = {
    'ZScoreBreakout':              'ZSB',
    'TrendFilteredZScore':         'TrendF',
    'LimitOrderZScoreBreakout':    'Limit',
    'TrailingExitZScoreBreakout':  'Trail',
    'TrailingBuyZScoreBreakout':   'TrBuy',
    'TrailingBothZScoreBreakout':  'TrBoth',
    'LimitOrderTrailingExit':      'LimTr',
    'LimitExitZScoreBreakout':     'LimExit',
}
CLIFF_SAFETY_RADIUS = 3

max_alpha = st.number_input("Max alpha cap % (0 = no cap)", value=0.0, step=100.0, format="%.0f",
                            help="Cap to exclude black-swan outliers (e.g. UVIX). 0 = disabled.")


@st.cache_data(ttl=3600)
def load_strategy_pivot(min_trades_val):
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql("""
            SELECT ticker, strategy, MAX(alpha_vs_spy) AS best_alpha
            FROM backtest_cache
            WHERE trades >= ?
            GROUP BY ticker, strategy
        """, conn, params=(min_trades_val,))
        bh = pd.read_sql("""
            SELECT ticker, MAX(asset_bh) AS bh
            FROM backtest_cache
            WHERE trades >= ? AND asset_bh IS NOT NULL
            GROUP BY ticker
        """, conn, params=(min_trades_val,))
    df['col'] = df['strategy'].map(STRAT_SHORT).fillna(df['strategy'])
    piv = df.pivot_table(index='ticker', columns='col', values='best_alpha', aggfunc='max')
    piv['max'] = piv.max(axis=1)
    piv = piv.join(bh.set_index('ticker')['bh'], how='left')
    return piv.sort_values('max', ascending=False).round(1)


@st.cache_data(ttl=86400, show_spinner="Loading cliff grid (slow if no sweep-end cache)...")
def load_cliff_grid_cached(min_trades_val):
    """One aggregated node per (ticker, strategy, version, window, z, tp, sl) — holds
    collapsed to best alpha. Served from the sweep-end kv cache when available."""
    from db_cache import load_cliff_grid
    return load_cliff_grid(min_trades_val)


@st.cache_data(ttl=3600)
def load_strategy_pivot_safe(min_trades_val, radius, min_neighbors, max_nodes=100, cliff_threshold=50):
    grid = load_cliff_grid_cached(min_trades_val)
    if grid.empty:
        return pd.DataFrame()

    # (ticker, strategy, version, window, z) → {(tp, sl): best alpha over holds}
    grids = {}
    for key, grp in grid.groupby(['ticker', 'strategy', 'version', 'window', 'z'], sort=False):
        grids[key] = dict(zip(zip(grp['take_profit'].astype(int), grp['stop_loss'].astype(int)),
                              grp['max_alpha']))

    # Walk down the top max_nodes per (ticker, strategy); keep the best one that
    # has >= min_neighbors positive-alpha neighbors within ±radius TP/SL AND no sharp cliff.
    results = []
    grid_sorted = grid.sort_values('max_alpha', ascending=False)
    for (ticker, strategy), grp in grid_sorted.groupby(['ticker', 'strategy'], sort=False):
        for c in grp.head(max_nodes).itertuples():
            g = grids[(c.ticker, c.strategy, c.version, c.window, c.z)]
            tp0, sl0 = int(c.take_profit), int(c.stop_loss)
            neighbors = [
                g.get((tp0 + dtp, sl0 + dsl), -999)
                for dtp in range(-radius, radius + 1)
                for dsl in range(-radius, radius + 1)
                if (dtp != 0 or dsl != 0)
            ]
            pos = sum(1 for a in neighbors if a > 0)
            if pos >= min_neighbors:
                min_neighbor_alpha = min([a for a in neighbors if a > 0], default=0)
                cliff_drop = c.max_alpha - min_neighbor_alpha
                if cliff_drop <= cliff_threshold:
                    results.append({'ticker': ticker, 'strategy': strategy, 'best_alpha': c.max_alpha})
                    break

    if not results:
        return pd.DataFrame()

    best = pd.DataFrame(results)
    best['col'] = best['strategy'].map(STRAT_SHORT).fillna(best['strategy'])
    piv = best.pivot_table(index='ticker', columns='col', values='best_alpha', aggfunc='max')
    piv['max'] = piv.max(axis=1)
    piv = piv.join(grid.groupby('ticker')['bh'].max(), how='left')
    return piv.sort_values('max', ascending=False).round(1)


def _apply_strat_pivot_filters(piv, exclude_ss, exclude_ix, ss_set, ix_set, max_alpha_cap, min_ret):
    if exclude_ss:
        piv = piv[~piv.index.isin(ss_set)]
    if exclude_ix:
        piv = piv[~piv.index.isin(ix_set)]
    piv = piv[piv['max'] >= min_ret]
    if max_alpha_cap > 0:
        piv = piv[piv['max'] <= max_alpha_cap]
    return piv


def _render_strat_pivot(piv):
    if piv.empty:
        st.info("No results.")
        return
    scols = [c for c in piv.columns if c not in ('max', 'bh')]
    col_cfg = {c: st.column_config.NumberColumn(c, format="%.1f%%") for c in scols}
    col_cfg['max'] = st.column_config.NumberColumn('Best α', format="%.1f%%")
    col_cfg['bh']  = st.column_config.NumberColumn('B&H %', format="%.1f%%")
    st.dataframe(piv, use_container_width=True, column_config=col_cfg)


_ss = load_single_stock_tickers()
_ix = load_index_tickers()

strat_piv = load_strategy_pivot(int(min_trades))
strat_piv = _apply_strat_pivot_filters(strat_piv, exclude_single_stock, exclude_index,
                                        _ss, _ix, max_alpha, min_return)
_render_strat_pivot(strat_piv)

st.divider()
st.subheader("Universe — Best Cliff-Safe Alpha by Strategy")
_grid = load_cliff_grid_cached(int(min_trades))
st.caption(f"Searching {len(_grid):,} qualified cliff-grid nodes ({_grid['ticker'].nunique()} tickers)")
cs_c1, cs_c2, cs_c3, cs_c4 = st.columns(4)
with cs_c1:
    cliff_radius = st.number_input("Cliff-safe radius", value=3, step=1, min_value=1,
                                   help="±N in TP/SL integer values. Use 3 for coarse grid, 2 for fine mesh.")
with cs_c2:
    min_cliff_neighbors = st.number_input("Min safe neighbors", value=4, step=1, min_value=1,
                                          help="How many ±radius neighbors must have alpha > 0.")
with cs_c3:
    max_nodes_to_check = st.number_input("Max nodes to check", value=100, step=10, min_value=10,
                                         help="Top N nodes per (ticker, strategy) to evaluate.")
with cs_c4:
    cliff_threshold = st.number_input("Cliff threshold (%)", value=50, step=10, min_value=0,
                                      help="Max alpha drop to neighbor. Higher = less strict, lower = reject more ridges.")
st.caption(f"Only nodes where ≥{int(min_cliff_neighbors)} neighbors within ±{int(cliff_radius)} TP/SL have alpha > 0 and drop < {int(cliff_threshold)}%")

safe_piv = load_strategy_pivot_safe(int(min_trades), int(cliff_radius), int(min_cliff_neighbors),
                                     int(max_nodes_to_check), int(cliff_threshold))
safe_piv = _apply_strat_pivot_filters(safe_piv, exclude_single_stock, exclude_index,
                                       _ss, _ix, max_alpha, min_return)
_render_strat_pivot(safe_piv)

st.divider()
st.subheader("Cliff Safety — Best vs Worst Neighbor")


@st.cache_data(ttl=3600)
def load_version_strategy_pairs():
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT DISTINCT version, strategy FROM backtest_cache ORDER BY version DESC"
        ).fetchall()


@st.cache_data(ttl=3600, show_spinner="Running cliff-box lookups...")
def load_cliff_safety(version_strategy_pairs):
    """Per (ticker, version, strategy): best node + worst alpha in its ±radius TP/SL,
    ±7h hold neighborhood. Same math as Checkpoint2 in run_optimization_sweep.py."""
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        for version, strategy in version_strategy_pairs:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM backtest_cache WHERE version=? AND strategy=? AND trades > 0",
                (version, strategy)
            ).fetchall()]
            for ticker in tickers:
                row = conn.execute("""
                    SELECT take_profit, stop_loss, max_hold_hours, window, z_score_threshold,
                           alpha_vs_spy, strategy_return, trades, win_rate
                    FROM backtest_cache
                    WHERE version=? AND ticker=? AND strategy=? AND trades > 0
                    ORDER BY alpha_vs_spy DESC LIMIT 1
                """, (version, ticker, strategy)).fetchone()
                if not row:
                    continue
                tp, sl, hold, window, z, alpha, ret, trades, win_rate = row
                tp, sl, hold, window = int(tp), int(sl), int(hold), int(window)
                worst = conn.execute("""
                    SELECT MIN(alpha_vs_spy) FROM backtest_cache
                    WHERE version=? AND ticker=? AND strategy=?
                      AND window=? AND z_score_threshold=?
                      AND take_profit    BETWEEN ? AND ?
                      AND stop_loss      BETWEEN ? AND ?
                      AND max_hold_hours BETWEEN ? AND ?
                      AND trades > 0
                """, (version, ticker, strategy, window, z,
                      tp - CLIFF_SAFETY_RADIUS, tp + CLIFF_SAFETY_RADIUS,
                      sl - CLIFF_SAFETY_RADIUS, sl + CLIFF_SAFETY_RADIUS,
                      hold - 7, hold + 7)).fetchone()[0]
                worst_neighbor = float(worst) if worst is not None else float(alpha)
                results.append({
                    'ticker': ticker, 'version': version, 'strategy': strategy,
                    'best_alpha': float(alpha), 'worst_neighbor': worst_neighbor,
                    'safe': worst_neighbor >= 0,
                    'take_profit': tp, 'stop_loss': sl, 'max_hold_hours': hold,
                    'window': window, 'z': float(z),
                    'strategy_return': float(ret), 'trades': int(trades), 'win_rate': float(win_rate),
                })
    return pd.DataFrame(results)


vs_pairs_all = [(v, s) for v, s in load_version_strategy_pairs() if not v.startswith('v1.')]
vs_labels_all = [f"{v} / {s}" for v, s in vs_pairs_all]
chosen_labels = st.multiselect("Versions / strategies", vs_labels_all, default=vs_labels_all)
chosen_pairs = tuple(vs_pairs_all[vs_labels_all.index(lbl)] for lbl in chosen_labels)

if not chosen_pairs:
    st.info("Select at least one version/strategy.")
else:
    safety_df = load_cliff_safety(chosen_pairs)
    if safety_df.empty:
        st.info("No data for the selected version/strategy pairs.")
    else:
        st.caption(f"{len(safety_df)} ticker × version/strategy rows")

        table_col_cfg = {
            'best_alpha':      st.column_config.NumberColumn('Best α', format="%.1f%%"),
            'worst_neighbor':  st.column_config.NumberColumn('Worst Neighbor', format="%.1f%%"),
            'strategy_return': st.column_config.NumberColumn('Return', format="%.1f%%"),
            'win_rate':        st.column_config.NumberColumn('Win %', format="%.1f%%"),
            'safe':            st.column_config.CheckboxColumn('Safe'),
        }
        st.dataframe(
            safety_df[['ticker', 'version', 'strategy', 'best_alpha', 'worst_neighbor', 'safe',
                       'take_profit', 'stop_loss', 'max_hold_hours', 'window', 'z',
                       'strategy_return', 'trades', 'win_rate']].sort_values('worst_neighbor'),
            use_container_width=True, hide_index=True, column_config=table_col_cfg,
        )

        st.markdown("**Pivot — worst neighbor (of each ticker's best node) vs. best α by version/strategy**")
        safety_df['col'] = safety_df['strategy'].map(STRAT_SHORT).fillna(safety_df['strategy']) + ' ' + safety_df['version']
        best_idx = safety_df.groupby('ticker')['best_alpha'].idxmax()
        per_ticker = safety_df.loc[best_idx, ['ticker', 'worst_neighbor', 'safe']].set_index('ticker')
        vs_piv = safety_df.pivot_table(index='ticker', columns='col', values='best_alpha', aggfunc='max')
        vs_cols = list(vs_piv.columns)
        vs_piv = per_ticker.join(vs_piv, how='right')
        vs_piv['max'] = vs_piv[vs_cols].max(axis=1)
        vs_piv = vs_piv.sort_values('worst_neighbor')

        piv_col_cfg = {c: st.column_config.NumberColumn(c, format="%.1f%%") for c in vs_cols}
        piv_col_cfg['worst_neighbor'] = st.column_config.NumberColumn('Worst Neighbor', format="%.1f%%")
        piv_col_cfg['max']  = st.column_config.NumberColumn('Best α', format="%.1f%%")
        piv_col_cfg['safe'] = st.column_config.CheckboxColumn('Safe')
        st.dataframe(vs_piv, use_container_width=True, column_config=piv_col_cfg)

st.divider()
st.subheader("Watchlist — Alpha by Strategy")


@st.cache_data(ttl=300)
def load_watchlist_pivot():
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql('''
            SELECT w.ticker, w.mode, w.strategy, w.version,
                   w.window, w.z_score_threshold, w.take_profit, w.stop_loss, w.max_hold_hours,
                   b.alpha_vs_spy, b.strategy_return, b.trades, b.win_rate
            FROM watch_list w
            LEFT JOIN backtest_cache b
                ON  b.ticker           = w.ticker
                AND b.version          = w.version
                AND b.strategy         = w.strategy
                AND b.window           = w.window
                AND b.take_profit      = w.take_profit
                AND b.stop_loss        = w.stop_loss
                AND b.max_hold_hours   = w.max_hold_hours
                AND b.z_score_threshold = w.z_score_threshold
            WHERE w.watchlist_id = 1
        ''', conn)
    strat_short = {
        'ZScoreBreakout':             'ZSB',
        'LimitOrderZScoreBreakout':   'Limit',
        'TrailingExitZScoreBreakout': 'Trail',
    }
    df['strat_col'] = df['strategy'].map(strat_short).fillna(df['strategy']) + ' ' + df['version']
    df['row'] = df['ticker'] + ' (' + df['mode'] + ')'
    pivot = df.pivot_table(index='row', columns='strat_col', values='alpha_vs_spy', aggfunc='max')
    pivot['best'] = pivot.max(axis=1)
    return pivot.sort_values('best', ascending=False).round(1)


wl_pivot = load_watchlist_pivot()
if not wl_pivot.empty:
    strat_cols = [c for c in wl_pivot.columns if c != 'best']
    col_cfg = {c: st.column_config.NumberColumn(c, format="%.1f%%") for c in strat_cols}
    col_cfg['best'] = st.column_config.NumberColumn('Best α', format="%.1f%%")
    st.dataframe(wl_pivot, use_container_width=True, column_config=col_cfg)


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
