import sqlite3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from pathlib import Path

DB_PATH = Path("./cache/trading_universe.db")

def plot_organic_growth(strategy_name="ZScore_Original", version="v1.2"):
    if not DB_PATH.exists():
        print("❌ Database not found. Run an optimization sweep first!")
        return

    # 1. Pull data from the database
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT take_profit, stop_loss, max_hold_hours, alpha_vs_spy, run_timestamp
        FROM backtest_cache
        WHERE strategy = ? AND version = ?
    """
    df = pd.read_sql_query(query, conn, params=(strategy_name, version))
    conn.close()

    if df.empty:
        print(f"❌ No data found for strategy '{strategy_name}' and version '{version}'")
        return

    # 2. Sort by timestamp to approximate generation progression
    df = df.sort_values("run_timestamp").reset_index(drop=True)
    
    # Bucket into generations based on data size chunks to visualize the "growth" steps
    total_points = len(df)
    # The first chunk is Phase 1 (Macro Scan), subsequent points are Phase 2 iterations
    macro_cutoff = min(50, int(total_points * 0.3)) 
    
    df["Generation"] = "Gen 2+ (Branching)"
    df.loc[:macro_cutoff, "Generation"] = "Gen 1 (Macro Scan)"

    # 3. Setup 3D Canvas
    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_subplot(111, projection='3d')

    # Style definitions
    colors = {"Gen 1 (Macro Scan)": "#3498db", "Gen 2+ (Branching)": "#2ecc71"}
    markers = {"Gen 1 (Macro Scan)": "o", "Gen 2+ (Branching)": "^"}
    
    # 4. Plot each generation layer
    for gen, group in df.groupby("Generation"):
        # Scale sizes by Alpha performance so highly profitable hubs physically stand out
        sizes = np.clip(group["alpha_vs_spy"] * 15, 20, 400) 
        
        ax.scatter(
            group["stop_loss"],          # X-Axis
            group["take_profit"],         # Y-Axis
            group["max_hold_hours"],      # Z-Axis
            c=colors[gen],
            marker=markers[gen],
            s=sizes,
            alpha=0.6,
            edgecolors='w',
            linewidths=0.5,
            label=gen
        )

    # 5. Labels and 3D space orientation
    ax.set_title(f"🛡️ 3D Organic Frontier Growth Landscape\nStrategy: {strategy_name} | Version: {version}", fontsize=14, pad=20)
    ax.set_xlabel("Stop Loss %", fontsize=11, labelpad=10)
    ax.set_ylabel("Take Profit %", fontsize=11, labelpad=10)
    ax.set_zlabel("Max Hold (Hours)", fontsize=11, labelpad=10)
    
    # Adjust tick spacing to match your clean 24h intervals on Z axis
    ax.set_zticks([24, 48, 72, 96, 120])
    
    ax.legend(loc="upper left", fontsize=10)
    ax.view_init(elev=25, azim=-135)  # Perfect isometric viewpoint angle

    plt.tight_layout()
    
    # Save the model visualization to your log folder
    output_path = Path("./logs") / f"3d_growth_topology_{strategy_name}.png"
    plt.savefig(output_path, dpi=200, bbox_inches='tight')
    print(f"✅ Beautiful 3D organic growth map successfully rendered to: {output_path}")
    plt.show()

if __name__ == "__main__":
    # Customize these to match the exact run you want to see
    plot_organic_growth(strategy_name="ZScore_Original", version="v1.2")