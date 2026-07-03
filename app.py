import streamlit as st
import json
import sqlite3
import os
import subprocess
import sys
import signal
import inspect
from pathlib import Path

import strategies

STRATEGY_OPTIONS = [
    name for name, obj in inspect.getmembers(strategies, inspect.isclass)
    if obj.__module__ == 'strategies' and issubclass(obj, strategies.BaseStrategy) and obj is not strategies.BaseStrategy
]

# Enforce clean workspace environment paths immediately on boot
DB_DIR = Path("./cache")
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "trading_universe.db"
TELEMETRY_PATH = "current_test.json"

st.set_page_config(layout="wide", page_title="Alpha Engine Configuration")

def init_db():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("CREATE TABLE IF NOT EXISTS active_workers (id TEXT PRIMARY KEY, pid INTEGER)")
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"🔴 Database initialization failure: {e}")

def load_config():
    default_config = {
        "version": "v1.2",
        "target_tickers": ["TQQQ", "SQQQ", "SPY"],
        "active_strategies": ["ZScoreBreakout"],
        "hyperparameters": {
            "windows": [20],
            "take_profits": [2, 4, 6, 8],
            "stop_losses": [1, 2, 3, 4],
            "hold_time_caps": [24, 48, 72, 96, 120]
        },
        "execution": {
            "max_generations": 4,
            "alpha_tolerance": -2.0
        }
    }
    if not os.path.exists("config.json"):
        save_config(default_config)
        return default_config
    try:
        with open("config.json", "r") as f:
            config = json.load(f)
        for key in default_config:
            if key not in config:
                config[key] = default_config[key]
        return config
    except Exception:
        return default_config

def save_config(config_dict):
    try:
        with open("config.json", "w") as f:
            json.dump(config_dict, f, indent=4)
    except IOError as e:
        st.error(f"Failed to save config.json: {e}")

def get_active_worker_pid():
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT pid FROM active_workers WHERE id = 'sweep_master'")
        row = cursor.fetchone()
        conn.close()
        if row:
            pid = row[0]
            try:
                os.kill(pid, 0) # Check OS process lifecycle trace
                return pid
            except OSError:
                clear_worker_pid()
                return None
    except Exception:
        return None
    return None

def save_worker_pid(pid):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO active_workers (id, pid) VALUES ('sweep_master', ?)", (pid,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def clear_worker_pid():
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM active_workers WHERE id = 'sweep_master'")
        conn.commit()
        conn.close()
    except Exception:
        pass

# --- Interface Layout Presentation Frame ---
st.title("⚙️ Master Core Optimization Settings")

init_db()
active_pid = get_active_worker_pid()
if active_pid:
    st.error(f"⚠️ SYSTEM STATUS: Optimization Suite actively running in background (PID: {active_pid}). Cluster processes fully engaged.")
else:
    st.success("✅ SYSTEM STATUS: Compute Engine Idle. Ready to deploy parallel worker pipelines.")

st.markdown("Configure global hyperparameters, strategy boundaries, and deploy parallel sweep worker pools across the cluster environment.")

db_config = load_config()

# --- Consolidated Configuration Form Entry Block ---
with st.form(key="global_config_form"):
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Runtime Meta Boundaries")
        version_string = st.text_input("Active Engine Version Tag", value=str(db_config.get("version", "v1.2")))
        ticker_string = st.text_input("Target Ticker Set (Comma Separated)", ", ".join(db_config.get("target_tickers", ["TQQQ"])))
        
        strategy_choices = st.multiselect(
            "Active Compute Strategies",
            STRATEGY_OPTIONS,
            default=[s for s in db_config.get("active_strategies", ["ZScoreBreakout"]) if s in STRATEGY_OPTIONS]
        )
        
        # Pull inner nested configuration attributes safely using .get() fallbacks
        exec_settings = db_config.get("execution", {"max_generations": 4, "alpha_tolerance": -2.0})
        max_gens = st.number_input("Max Search Generations", min_value=0, value=int(exec_settings.get("max_generations", 4)))
        alpha_tol = st.number_input("Alpha Drop Prune Tolerance (%)", value=float(exec_settings.get("alpha_tolerance", -2.0)), step=0.5)

    with col2:
        st.subheader("Hyperparameter Arrays (Comma-Separated Matrices)")
        hp_settings = db_config.get("hyperparameters", {"windows": [20], "take_profits": [4], "stop_losses": [2], "hold_time_caps": [48]})
        
        windows_input = st.text_input("Indicator Lookback Windows", ", ".join(map(str, hp_settings.get("windows", [20]))))
        tp_input = st.text_input("Take Profits Baseline (%)", ", ".join(map(str, hp_settings.get("take_profits", [2, 4, 6, 8]))))
        sl_input = st.text_input("Stop Losses Baseline (%)", ", ".join(map(str, hp_settings.get("stop_losses", [1, 2, 3, 4]))))
        hold_input = st.text_input("Max Hold Horizons (Hours)", ", ".join(map(str, hp_settings.get("hold_time_caps", [24, 48, 72, 96, 120]))))
        zthresh_input = st.text_input("Z-Score Thresholds", ", ".join(map(str, hp_settings.get("z_score_thresholds", [2.0]))))

    submit_button = st.form_submit_button(label="💾 Lock Configuration Parameters & Update Workspace File", use_container_width=True)

if submit_button:
    try:
        # Merge into the loaded config rather than rebuilding from scratch — otherwise
        # fields this form doesn't manage (max_workers, fixed_stop_loss, any future key)
        # get silently dropped on every save.
        updated_config = dict(db_config)
        updated_config["version"] = version_string.strip()
        updated_config["target_tickers"] = [t.strip().upper() for t in ticker_string.split(",") if t.strip()]
        updated_config["active_strategies"] = strategy_choices
        updated_config["hyperparameters"] = {
            **db_config.get("hyperparameters", {}),
            "z_score_thresholds": [float(x.strip()) for x in zthresh_input.split(",") if x.strip()],
            "windows": [int(x.strip()) for x in windows_input.split(",") if x.strip()],
            "take_profits": [int(x.strip()) for x in tp_input.split(",") if x.strip()],
            "stop_losses": [int(x.strip()) for x in sl_input.split(",") if x.strip()],
            "hold_time_caps": [int(x.strip()) for x in hold_input.split(",") if x.strip()]
        }
        updated_config["execution"] = {
            **db_config.get("execution", {}),
            "max_generations": int(max_gens),
            "alpha_tolerance": float(alpha_tol)
        }
        save_config(updated_config)
        st.success("Global configuration successfully written to database and configuration file sync layer!")
        st.rerun()
    except Exception as parse_err:
        st.error(f"Formatting Error: Ensure matrix lists contain numeric integers only. Details: {parse_err}")

st.markdown("---")
st.subheader("Worker Execution Control Deck")

if active_pid:
    st.markdown("An optimization sweep is currently executing. Clicking below sends an OS break interrupt sequence to collapse all active subprocess structures.")
    if st.button("🛑 Terminate Active Optimization Process (Ctrl+C Equivalent)", use_container_width=True, type="primary"):
        try:
            if sys.platform == "win32":
                os.kill(active_pid, signal.CTRL_C_EVENT)
            else:
                os.kill(active_pid, signal.SIGTERM)
        except Exception:
            try: os.kill(active_pid, signal.SIGKILL)
            except Exception: pass
        
        clear_worker_pid()
        for file in ["current_test.json", "active_phase_grid.json"]:
            if os.path.exists(file):
                try: os.remove(file)
                except Exception: pass
        st.success("Cluster computation loops successfully closed down. UI states reset.")
        st.rerun()
else:
    if st.button("🚀 Launch Background Compute Optimization Matrix Pool", use_container_width=True):
        st.info("Spawning detached search subprocess loops...")
        proc = subprocess.Popen(
            [sys.executable, "run_optimization_sweep.py"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        save_worker_pid(proc.pid)
        st.success(f"Engine deployed as System PID: {proc.pid}! Progress tracking is now live.")
        st.rerun()