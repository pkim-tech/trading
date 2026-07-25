import sqlite3
from collections import defaultdict
from datetime import date

import pandas as pd
import streamlit as st

DB_PATH = "./cache/live/trading_live.db"
MODES = ["paper", "dry_run", "live", "unattributed"]

st.set_page_config(page_title="Coverage", layout="wide")
st.title("Coverage Compass")
st.caption(
    "Expected-vs-actual scenario tracking. Any deviation from a documented expectation "
    "must carry a reason -- an unexplained deviation is itself the actionable finding."
)


@st.cache_data(ttl=30)
def load_deviations(unexplained_only=False):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        q = "SELECT * FROM coverage_deviations"
        if unexplained_only:
            q += " WHERE reason IS NULL"
        q += " ORDER BY id DESC LIMIT 500"
        return [dict(r) for r in c.execute(q).fetchall()]


@st.cache_data(ttl=30)
def load_scenario_expectations():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM scenario_expectations WHERE active = 1 ORDER BY id"
        ).fetchall()]


@st.cache_data(ttl=30)
def load_coverage_events():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM coverage_events ORDER BY id DESC LIMIT 100000"
        ).fetchall()]


@st.cache_data(ttl=30)
def load_node_labels():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, ticker, strategy, version, window, account FROM watch_list").fetchall()
    return {
        r["id"]: f"{r['ticker']} {r['strategy']}/{r['version']} w{r['window']} ({r['account'] or 'no-acct'})"
        for r in rows
    }


def explain(deviation_id, reason):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE coverage_deviations SET reason=?, reason_by='streamlit', reason_ts=datetime('now') WHERE id=?",
            (reason, deviation_id),
        )
        c.commit()
    load_deviations.clear()


node_labels = load_node_labels()

# --- Section 1: unexplained deviations, most actionable first ---
st.header("Unexplained Deviations")
unexplained = load_deviations(unexplained_only=True)
if not unexplained:
    st.success("No unexplained deviations.")
else:
    st.error(f"{len(unexplained)} deviation(s) with no captured reason.")
    for d in unexplained:
        node_label = node_labels.get(d["node_id"], "")
        with st.expander(
            f"[{d['id']}] {d['check_date']}  {d['scenario_key']}  "
            f"{d['ticker'] or node_label or ''}",
            expanded=False,
        ):
            st.write(f"**Expected:** {d['expected_outcome']}")
            st.write(f"**Actual:** {d['actual_summary']}")
            if node_label:
                st.caption(f"Node: {node_label}")
            with st.form(key=f"explain_{d['id']}"):
                reason = st.text_input("Reason", key=f"reason_{d['id']}")
                if st.form_submit_button("Explain") and reason.strip():
                    explain(d["id"], reason.strip())
                    st.rerun()

st.divider()

# --- Section 2: today's scenario status ---
st.header("Today's Scenarios")
today = date.today().isoformat()
today_deviations = {d["scenario_key"]: d for d in load_deviations() if d["check_date"] == today}
expectations = load_scenario_expectations()

rows = []
for s in expectations:
    dev = today_deviations.get(s["scenario_key"])
    if dev is None:
        status = "✓ met"
        actual = ""
        reason = ""
    elif dev["reason"]:
        status = "✗ explained"
        actual = dev["actual_summary"]
        reason = dev["reason"]
    else:
        status = "✗ UNEXPLAINED"
        actual = dev["actual_summary"]
        reason = ""
    rows.append({
        "Scenario":  s["scenario_key"],
        "Ticker":    s["ticker"] or "",
        "Node":      node_labels.get(s["node_id"], ""),
        "Mode":      s["mode"] or "any",
        "Frequency": s["expected_frequency"],
        "Status":    status,
        "Expected":  s["expected_outcome"],
        "Actual":    actual,
        "Reason":    reason,
    })

if rows:
    st.dataframe(pd.DataFrame(rows), hide_index=True, height=35 * (len(rows) + 1) + 10)
else:
    st.info("No scenario_expectations rows -- run scripts/seed_scenario_expectations.py.")

st.divider()

# --- Section 3: coverage matrix (scenario x mode) ---
st.header("Coverage Matrix")
events = load_coverage_events()
cells = defaultdict(list)
for e in events:
    cells[(e["scenario_key"], e["mode"])].append(e)
scenario_keys = sorted({e["scenario_key"] for e in events})

if scenario_keys:
    matrix_rows = []
    for key in scenario_keys:
        row = {"Scenario": key}
        for mode in MODES:
            cell = cells.get((key, mode), [])
            row[mode] = f"{len(cell)}x last={cell[0]['ts'][:10]} ({cell[0]['result']})" if cell else "—"
        matrix_rows.append(row)
    st.dataframe(pd.DataFrame(matrix_rows), hide_index=True, height=35 * (len(matrix_rows) + 1) + 10)
else:
    st.info("No coverage_events rows yet.")

st.divider()

# --- Section 4: drill-down ---
st.header("Drill Down")
options = ["(select a scenario)"] + scenario_keys
picked = st.selectbox("Scenario", options)
if picked != "(select a scenario)":
    detail_rows = [
        {
            "Time":     e["ts"],
            "Mode":     e["mode"],
            "Ticker":   e["ticker"] or "",
            "Strategy": e.get("strategy_type") or "",
            "Node":     node_labels.get(e["node_id"], ""),
            "Result":   e["result"],
            "Detail":   e["detail"],
        }
        for e in events if e["scenario_key"] == picked
    ][:200]
    st.dataframe(pd.DataFrame(detail_rows), hide_index=True, height=35 * (len(detail_rows) + 1) + 10)

st.caption("Refresh the page to pull latest data.")
