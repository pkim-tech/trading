import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import plotly.graph_objects as go
from db_cache import get_kv, set_kv

DB_PATH = "./cache/research/trading_universe.db"
TELEMETRY_PATH = "current_test.json"
PHASE_GRID_PATH = "active_phase_grid.json"

st.set_page_config(layout="wide", page_title="Spatial Topology Space")


@st.cache_data(ttl=86400)
def load_dropdown_options():
    if not os.path.exists(DB_PATH):
        return [], {}, {}
    versions = get_kv("versions")
    if versions is None:
        with sqlite3.connect(DB_PATH) as conn:
            versions = [r[0] for r in conn.execute(
                "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
            ).fetchall()]
    tickers_by_version = {}
    strats_by_version_ticker = {}
    for v in versions:
        tickers = get_kv(f"tickers_{v}")
        if tickers is None:
            with sqlite3.connect(DB_PATH) as conn:
                tickers = [r[0] for r in conn.execute(
                    "SELECT DISTINCT ticker FROM backtest_cache WHERE version = ? ORDER BY ticker", (v,)
                ).fetchall()]
        tickers_by_version[v] = tickers
        strats = get_kv(f"strategies_{v}") or ["ZScoreBreakout"]
        for t in tickers:
            strats_by_version_ticker[(v, t)] = strats
    return versions, tickers_by_version, strats_by_version_ticker


@st.cache_data(ttl=86400)
def load_slice(version, ticker, strategy):
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """SELECT window, max_hold_hours, take_profit, stop_loss,
                      COALESCE(z_score_threshold, 2.0) as z_score_threshold,
                      trades, win_rate, strategy_return, alpha_vs_spy, asset_bh
               FROM backtest_cache
               WHERE version = ? AND ticker = ? AND strategy = ?""",
            conn, params=(version, ticker, strategy)
        )

def load_planned_nodes():
    if not os.path.exists(PHASE_GRID_PATH):
        return pd.DataFrame()
    try:
        with open(PHASE_GRID_PATH, "r") as f:
            data = json.load(f)
        nodes = data.get("nodes", [])
        if not nodes:
            return pd.DataFrame()
        df = pd.DataFrame(nodes)
        return df
    except Exception:
        return pd.DataFrame()

def get_active_telemetry():
    if os.path.exists(TELEMETRY_PATH):
        try:
            with open(TELEMETRY_PATH, "r") as f: return json.load(f)
        except Exception: return None
    return None

st.title("Spatial Topology")

versions, tickers_by_version, strats_by_version_ticker = load_dropdown_options()
active_node = get_active_telemetry()

if not versions:
    st.warning("No completed hyperparameter records found. Initialize optimization runs via the main config view.")
    st.stop()
else:
    t_top = st.session_state.pop("target_topology", {})

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        default_ver = t_top.get("version") if t_top.get("version") in versions else versions[0]
        selected_ver = st.selectbox("Version", versions, index=versions.index(default_ver))
    with col_b:
        ticker_opts = tickers_by_version.get(selected_ver, [])
        default_ticker = t_top.get("ticker") if t_top.get("ticker") in ticker_opts else ticker_opts[0]
        selected_ticker = st.selectbox("Ticker", ticker_opts, index=ticker_opts.index(default_ticker))
    with col_c:
        strat_opts = sorted(strats_by_version_ticker.get((selected_ver, selected_ticker), []))
        default_strat = t_top.get("strategy") if t_top.get("strategy") in strat_opts else strat_opts[0]
        selected_strat = st.selectbox("Strategy", strat_opts, index=strat_opts.index(default_strat))

    df_all = load_slice(selected_ver, selected_ticker, selected_strat)

    zt_opts = sorted(df_all["z_score_threshold"].unique()) if not df_all.empty else [2.0]
    if len(zt_opts) > 1:
        default_zt = t_top.get("z_score_threshold", 2.0) if t_top.get("z_score_threshold") in zt_opts else zt_opts[0]
        selected_zt = st.selectbox("Z-Score Threshold", zt_opts, index=zt_opts.index(default_zt))
    else:
        selected_zt = zt_opts[0]

    df_filtered = df_all[df_all["z_score_threshold"] == selected_zt].copy() if not df_all.empty else df_all

    st.markdown("---")
    
    # --- 4D Dynamic Axis Controller Levers ---
    st.subheader("🕹️ High-Dimensional Projections Slicing Matrix")
    dimension_options = {
        "Take Profit %": "take_profit",
        "Stop Loss %": "stop_loss",
        "Max Hold Hours": "max_hold_hours",
        "Lookback Window": "window"
    }
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        x_axis_label = st.selectbox("X-Axis Spatial Mapping", list(dimension_options.keys()), index=0)
    with c2:
        y_axis_label = st.selectbox("Y-Axis Spatial Mapping", list(dimension_options.keys()), index=1)
    with c3:
        z_axis_label = st.selectbox("Z-Axis Spatial Mapping", list(dimension_options.keys()), index=2)
    with c4:
        slice_label = st.selectbox("4th-Dimension Isolation Filter", list(dimension_options.keys()), index=3)

    if len({x_axis_label, y_axis_label, z_axis_label, slice_label}) < 4:
        st.error("Invalid state. Each parameter field dimension map option must map uniquely to a single axis or slicing engine filter.")
    else:
        x_col = dimension_options[x_axis_label]
        y_col = dimension_options[y_axis_label]
        z_col = dimension_options[z_axis_label]
        s_col = dimension_options[slice_label]

        available_slice_values = sorted([v for v in df_filtered[s_col].unique() if v is not None and pd.notna(v)])
        
        if len(available_slice_values) > 1:
            # Safely render the slider if there is a valid mathematical range
            selected_slice_val = st.select_slider(
                f"Isolate Plane Slice Range for {slice_label}", 
                options=available_slice_values
            )
            df_plot_base = df_filtered[df_filtered[s_col] == selected_slice_val].copy()
            
        elif len(available_slice_values) == 1:
            # If only 1 coordinate exists, auto-lock to it without drawing a broken slider component
            selected_slice_val = available_slice_values[0]
            st.info(f"Fixed Horizon Layer Locked: {slice_label} = {selected_slice_val}")
            df_plot_base = df_filtered[df_filtered[s_col] == selected_slice_val].copy()
            
        else:
            # Fallback if the database slice is completely empty
            df_plot_base = df_filtered.copy()

        # --- Re-architected Filter Workspace Layout ---
        plot_col, filter_col = st.columns([5, 1])
        
        # --- Cleaned Workspace Layout Filter Engine ---
        with filter_col:
            st.markdown("### 🎛️ Alpha Filter")
            
            # 🛡️ Initialize default fallback to prevent NameErrors down-page
            lock_axis_scaling = False
            
            if not df_plot_base.empty:
                true_min = float(df_plot_base['alpha_vs_spy'].min())
                true_max = float(df_plot_base['alpha_vs_spy'].max())

                if true_min == true_max:
                    true_max = true_min + 1.0

                invert_slider = st.checkbox("Invert slider", value=False,
                    help="Inverted: slider sets a MAX cutoff — drag left to hide high-alpha nodes.")

                if invert_slider:
                    alpha_cutoff = st.slider(
                        "Max Alpha vs SPY (%)",
                        min_value=float(true_min),
                        max_value=float(true_max),
                        value=float(true_max),
                        step=0.5,
                        help="Nodes above this threshold will be hidden from the map."
                    )
                    df_plot_final = df_plot_base[df_plot_base['alpha_vs_spy'] <= alpha_cutoff].copy()
                else:
                    alpha_cutoff = st.slider(
                        "Min Alpha vs SPY (%)",
                        min_value=float(true_min),
                        max_value=float(true_max),
                        value=float(true_min),
                        step=0.5,
                        help="Nodes falling below this threshold will be hidden from the map."
                    )
                    df_plot_final = df_plot_base[df_plot_base['alpha_vs_spy'] >= alpha_cutoff].copy()
                
                # 💎 RESTORED CRITICAL WIDGET: Kept variable alive inside the data path
                lock_axis_scaling = st.checkbox(
                    "Freeze Grid Bounds",
                    value=False,
                    help="When checked, grid bounds remain static so nodes vanish in place."
                )
                
                st.metric("Visible Nodes", f"{len(df_plot_final)} / {len(df_plot_base)}")
                st.caption(f"Absolute Floor: {true_min:+.1f}% | Absolute Ceiling: {true_max:+.1f}%")
            else:
                df_plot_final = df_plot_base.copy()
                lock_axis_scaling = False # Safe fallback
                st.caption("No points available in current configuration.")
                

        # --- Calculate Anchored Viewport Ranges ---
        # Always pin axes to the pre-filter (df_plot_base) range so nodes vanish in place
        # rather than the grid reshaping around the filtered subset.
        if not df_plot_base.empty:
            def _padded(lo, hi):
                pad = (hi - lo) * 0.05 if hi != lo else 1.0
                return [lo - pad, hi + pad]
            x_range = _padded(float(df_plot_base[x_col].min()), float(df_plot_base[x_col].max()))
            y_range = _padded(float(df_plot_base[y_col].min()), float(df_plot_base[y_col].max()))
            z_range = _padded(float(df_plot_base[z_col].min()), float(df_plot_base[z_col].max()))
        else:
            x_range, y_range, z_range = None, None, None

        # --- Plotly 3D Canvas Engine ---
        fig = go.Figure()
        if not df_plot_final.empty:
            hover_strings = [
                f"<b>📍 COORDINATE NODE</b><br>"
                f"Lookback Window: {w}w<br>"
                f"Take Profit: {tp}%<br>"
                f"Stop Loss: {sl}%<br>"
                f"Max Hold: {h}h<br>"
                f"--------------------<br>"
                f"📈 Alpha vs SPY: {alpha:+.2f}%<br>"
                f"💵 Sim Return: {r:+.2f}%<br>"
                f"🎯 Win Rate: {wr:.1f}%<br>"
                f"📊 Total Trades: {t}"
                for w, tp, sl, h, alpha, r, wr, t in zip(
                    df_plot_final["window"], df_plot_final["take_profit"], df_plot_final["stop_loss"],
                    df_plot_final["max_hold_hours"], df_plot_final["alpha_vs_spy"], df_plot_final["strategy_return"],
                    df_plot_final["win_rate"], df_plot_final["trades"]
                )
            ]

            fig.add_trace(go.Scatter3d(
                x=df_plot_final[x_col], y=df_plot_final[y_col], z=df_plot_final[z_col],
                mode='markers',
                marker=dict(
                    size=6, color=df_plot_final["alpha_vs_spy"], colorscale='RdYlGn', cmid=0.0,
                    cmin=float(df_plot_base['alpha_vs_spy'].min()),
                    cmax=float(df_plot_base['alpha_vs_spy'].max()),
                    colorbar=dict(title="Alpha vs SPY %", x=1.05), opacity=0.8,
                    line=dict(color='black', width=0.3)
                ),
                text=hover_strings,
                hoverinfo='text'
            ))

        df_planned = load_planned_nodes()
        if not df_planned.empty and all(c in df_planned.columns for c in [x_col, y_col, z_col]):
            completed_keys = set(zip(df_plot_base[x_col], df_plot_base[y_col], df_plot_base[z_col]))
            df_unrun = df_planned[
                ~df_planned.apply(lambda r: (r[x_col], r[y_col], r[z_col]) in completed_keys, axis=1)
            ]
            if not df_unrun.empty:
                fig.add_trace(go.Scatter3d(
                    x=df_unrun[x_col], y=df_unrun[y_col], z=df_unrun[z_col],
                    mode='markers',
                    marker=dict(size=3, color='royalblue', opacity=0.4),
                    name='Planned (unrun)',
                    hoverinfo='skip'
                ))

        fig.update_layout(
            margin=dict(l=0, r=0, b=0, t=0),
            scene=dict(
                xaxis_title=x_axis_label, 
                yaxis_title=y_axis_label, 
                zaxis_title=z_axis_label,
                xaxis=dict(range=x_range) if x_range else dict(),
                yaxis=dict(range=y_range) if y_range else dict(),
                zaxis=dict(range=z_range) if z_range else dict()
            ),
            height=650,
            uirevision='constant'
        )
        
        with plot_col:
            st.plotly_chart(fig, use_container_width=True, key="spatial_3d_cube")

        # --- Unified Real-time Monitor HUD and Top Matrix Leaderboard ---
        col_hud, col_lead = st.columns([1.2, 1.8])
        
        with col_hud:
            st.subheader("Telemetry Engine Matrix")
            
            if not df_filtered.empty:
                st.markdown("### 🎯 Parametric Vector Query")
                
                # Grouped tiny select options targeting individual numerical axes
                sub_c1, sub_c2 = st.columns(2)
                with sub_c1:
                    q_tp = st.selectbox("Query Target TP %", sorted(df_filtered["take_profit"].unique()))
                    q_win = st.selectbox("Query Lookback Window", sorted(df_filtered["window"].unique()))
                with sub_c2:
                    q_sl = st.selectbox("Query Target SL %", sorted(df_filtered["stop_loss"].unique()))
                    q_hold = st.selectbox("Query Max Hold Hours", sorted(df_filtered["max_hold_hours"].unique()))
                
                # Instantly isolate the exact cross-section out of the thousands of points
                matched_rows = df_filtered[
                    (df_filtered["take_profit"] == q_tp) & 
                    (df_filtered["stop_loss"] == q_sl) & 
                    (df_filtered["window"] == q_win) & 
                    (df_filtered["max_hold_hours"] == q_hold)
                ]
                
                st.markdown("---")
                if not matched_rows.empty:
                    clicked_row = matched_rows.iloc[0]
                    st.metric("Alpha Performance", f"{clicked_row['alpha_vs_spy']:+.2f}%")
                    st.markdown(f"**Sim Return:** {clicked_row['strategy_return']:+.2f}% | **Win Rate:** {clicked_row['win_rate']:.1f}%")
                    st.markdown(f"**Total Trades executed:** {int(clicked_row['trades'])}")
                    
                    if st.button("📥 Load Isolated Node into Inspector", use_container_width=True, key="click_transmit"):
                        st.session_state["target_node"] = {
                            "ticker": selected_ticker, "strategy": selected_strat, "version": selected_ver,
                            "window": int(clicked_row["window"]), "take_profit": int(clicked_row["take_profit"]),
                            "stop_loss": int(clicked_row["stop_loss"]), "max_hold_hours": int(clicked_row["max_hold_hours"])
                        }
                        st.switch_page("pages/2_Node_Inspector.py")
                else:
                    st.warning("No data row matches this specific vector coordinate layout.")
            
            elif active_node and active_node.get("ticker") == selected_ticker:
                st.metric("📋 Currently Processing", f"{active_node.get('ticker')}")
                st.metric("🎯 Sub-Routine Focus Zone", f"TP: {active_node.get('take_profit')}% | SL: {active_node.get('stop_loss')}%")
            else:
                st.success("⚡ Optimization Process Idle.")

        with col_lead:
            st.subheader(f"🏆 Top Performance Frontier — {selected_ticker}")
            if not df_filtered.empty:
                df_leaderboard = df_plot_base if not df_plot_base.empty else df_filtered
                top_performers = df_leaderboard.nlargest(5, "alpha_vs_spy")[
                    ["take_profit", "stop_loss", "max_hold_hours", "window", "strategy_return", "win_rate", "trades", "asset_bh", "alpha_vs_spy"]
                ].reset_index(drop=True)
                
                st.dataframe(top_performers.style.format({
                    "take_profit": "{:,.0f}%", "stop_loss": "{:,.0f}%", "max_hold_hours": "{:,.0f}h", "window": "{:,.0f}w",
                    "strategy_return": "{:+.2f}%", "win_rate": "{:.1f}%", "trades": "{:,.0f}", "asset_bh": "{:+.2f}%", "alpha_vs_spy": "{:+.2f}%"
                }), hide_index=False, use_container_width=True)

                st.markdown("### 🔍 Inspect a Frontier Node")
                selected_idx = st.selectbox(
                    "Select a row number from the table above to load into the Node Inspector:",
                    options=list(top_performers.index),
                    format_func=lambda x: f"Row {x}: TP={top_performers.loc[x, 'take_profit']}% | SL={top_performers.loc[x, 'stop_loss']}% | Window={top_performers.loc[x, 'window']}w"
                )
                
                if st.button("🔎 Transmit Chosen Node Coordinates to Inspector", use_container_width=True):
                    clicked_node = top_performers.iloc[selected_idx]
                    st.session_state["target_node"] = {
                        "ticker": selected_ticker, "strategy": selected_strat, "version": selected_ver,
                        "window": int(clicked_node["window"]), "take_profit": int(clicked_node["take_profit"]),
                        "stop_loss": int(clicked_node["stop_loss"]), "max_hold_hours": int(clicked_node["max_hold_hours"])
                    }
                    st.switch_page("pages/2_Node_Inspector.py")