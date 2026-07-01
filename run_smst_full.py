import json
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from run_optimization_sweep import (
    init_idempotent_db, compute_bh_returns, run_phase3_full,
    refresh_dropdown_cache, refresh_pivot_cache
)

init_idempotent_db()

with open("config.json") as f:
    config = json.load(f)

hp             = config["hyperparameters"]
config_version = config["version"]
max_workers    = config.get("execution", {}).get("max_workers", 6)
run_timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

asset_bh, spy_bh = compute_bh_returns("SMST")

with ProcessPoolExecutor(max_workers=max_workers) as pool:
    run_phase3_full(pool, "SMST", "ZScoreBreakout", config_version,
                    hp, spy_bh, asset_bh, run_timestamp)

refresh_dropdown_cache()
refresh_pivot_cache()
print("Done.")
