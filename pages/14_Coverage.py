import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.coverage_check import CHECKERS  # pure dict of checker functions, no side effects/Slack config
from scripts.coverage_registry import (
    REGISTRY, compute_status, compute_mode_statuses, offline_proof_for, STATUS_ORDER, MODES as GRID_MODES,
)

OFFLINE_PROOF_EMOJI = {'event-asserted': '✅', 'behavior-only': '🟡', 'none': '⬜'}

DB_PATH = "./cache/live/trading_live.db"
MODES = ["paper", "dry_run", "live", "unattributed"]
STATUS_LABEL = {
    'deviation-unexplained': '🟥 UNEXPLAINED deviation (real, unresolved failure)',
    'not-instrumented': '⬜ Not instrumented', 'wired-never-fired': '🟥 Wired, never fired',
    'live-attempt-failed': '🟥 Live attempt, no good outcome',
    'dry_run-attempt-failed': '🟥 Dry-run attempt, no good outcome',
    'paper-attempt-failed': '🟥 Paper attempt, no good outcome',
    'paper-only': '🟨 Paper only', 'dry_run-only': '🟧 Dry-run only',
    'offline-only': '⬛ Offline-only (by design)', 'verified-live': '🟩 Verified live',
}
# Heatmap row-background per status -- worst (red) to best (green), matching STATUS_LABEL's emoji.
STATUS_COLOR = {
    STATUS_LABEL['deviation-unexplained']:   '#e6736e',
    STATUS_LABEL['not-instrumented']:        '#e2e2e2',
    STATUS_LABEL['wired-never-fired']:       '#f5b7b1',
    STATUS_LABEL['live-attempt-failed']:     '#f5b7b1',
    STATUS_LABEL['dry_run-attempt-failed']:  '#f5b7b1',
    STATUS_LABEL['paper-attempt-failed']:    '#f5b7b1',
    STATUS_LABEL['paper-only']:              '#fdebd0',
    STATUS_LABEL['dry_run-only']:            '#fce4ba',
    STATUS_LABEL['offline-only']:            '#cfcfcf',
    STATUS_LABEL['verified-live']:           '#c9e8c4',
}

# Per-mode-cell emoji/color -- keyed by compute_mode_statuses()'s raw status_key
# (distinct from STATUS_LABEL above, which is keyed by compute_status()'s overall
# label string). 'not-applicable' (this REGISTRY row's mode_filter excludes the
# mode, or the mechanism isn't mode-scoped at all) is deliberately a light neutral
# color, not red -- it isn't a gap, it's "doesn't apply here by design."
MODE_EMOJI = {
    'verified': '🟩', 'verified-live': '🟩',
    'attempt-failed': '🟥', 'deviation-unexplained': '🟥',
    'wired-never-fired': '⬜',
    'not-applicable': '▪️',
    'not-instrumented': '⬛', 'offline-only': '⬛',
}
MODE_CELL_COLOR = {
    'verified': '#c9e8c4', 'verified-live': '#c9e8c4',
    'attempt-failed': '#f5b7b1', 'deviation-unexplained': '#e6736e',
    'wired-never-fired': '#f5b7b1',
    'not-applicable': '#f0f0f0',
    'not-instrumented': '#e2e2e2', 'offline-only': '#cfcfcf',
}
MODE_COL_LABEL = {'paper': 'Paper', 'dry_run': 'Dry-run', 'live': 'Live'}


def _heat_row_and_mode_cells(row):
    """Row-wide color from the overall Status for every column except the 3
    per-mode columns, which get their own independent color from that row's
    real per-mode evidence -- a row can be overall 'verified-live' while still
    showing e.g. Paper as never-fired, which a single whole-row color would hide."""
    idx = row.name
    mode_info = grid_mode_status[idx]
    overall_color = STATUS_COLOR.get(row['Status'], '')
    styles = []
    for col in row.index:
        if col in MODE_COL_LABEL.values():
            mode = [k for k, v in MODE_COL_LABEL.items() if v == col][0]
            status_key = mode_info[mode][0]
            styles.append(f'background-color: {MODE_CELL_COLOR.get(status_key, "")}; color: #111')
        else:
            styles.append(f'background-color: {overall_color}; color: #111')
    return styles

st.set_page_config(page_title="Coverage", layout="wide")
st.title("Coverage Compass")
st.caption(
    "Expected-vs-actual scenario tracking. Any deviation from a documented expectation "
    "must carry a reason -- an unexplained deviation is itself the actionable finding."
)


@st.cache_data(ttl=30)
def load_accountability_grid():
    rows = []
    mode_status_by_row = []
    for r in REGISTRY:
        status, detail = compute_status(r)
        mode_statuses = compute_mode_statuses(r)
        mode_status_by_row.append(mode_statuses)
        compact = " ".join(f"{m[0].upper()}{MODE_EMOJI.get(mode_statuses[m][0], '?')}" for m in GRID_MODES)
        proof, proof_detail = offline_proof_for(r.get('scenario_key'), r.get('mode_filter'))
        rows.append({
            "_order": STATUS_ORDER[status],
            "Status": STATUS_LABEL[status],
            "P/D/L": compact,
            "Scenario": r['scenario'],
            "Paper": f"{MODE_EMOJI.get(mode_statuses['paper'][0], '?')} {mode_statuses['paper'][1]}".strip(),
            "Dry-run": f"{MODE_EMOJI.get(mode_statuses['dry_run'][0], '?')} {mode_statuses['dry_run'][1]}".strip(),
            "Live": f"{MODE_EMOJI.get(mode_statuses['live'][0], '?')} {mode_statuses['live'][1]}".strip(),
            "Offline proof": f"{OFFLINE_PROOF_EMOJI.get(proof, '?')} {proof_detail}".strip(),
            "Code path": r['code_path'],
            "Offline coverage": r['offline_coverage'],
            "Notes": r['notes'],
        })
    order = sorted(range(len(rows)), key=lambda i: rows[i]["_order"])
    rows = [rows[i] for i in order]
    mode_status_by_row = [mode_status_by_row[i] for i in order]
    for r in rows:
        del r["_order"]
    return rows, mode_status_by_row


# --- Section 0: trade-flow test accountability grid ---
st.header("Trade-Flow Test Accountability Grid")
st.caption(
    "Every real logic branch that needs to be proven correct in paper/dry_run/live, computed live "
    "from coverage_events/coverage_deviations -- never hand-typed, so it can't silently go stale. "
    "Worst-status-first: Not instrumented / Wired-never-fired are the real gaps. Paper/Dry-run/Live "
    "columns are colored independently per cell -- 'P/D/L' is the same thing compressed to one glance."
)
grid_rows, grid_mode_status = load_accountability_grid()
grid_counts = defaultdict(int)
for r in grid_rows:
    grid_counts[r["Status"]] += 1
st.write(" &nbsp;&nbsp; ".join(f"{k}: **{v}**" for k, v in
         sorted(grid_counts.items(), key=lambda kv: STATUS_ORDER[
             [s for s, lbl in STATUS_LABEL.items() if lbl == kv[0]][0]])))
grid_df = pd.DataFrame(grid_rows).reset_index(drop=True)
st.dataframe(grid_df.style.apply(_heat_row_and_mode_cells, axis=1), hide_index=True,
             height=35 * (len(grid_rows) + 1) + 10)

st.divider()


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

# --- Section 1b: deviation history (explained + unexplained) ---
st.header("Deviation History")
history = load_deviations(unexplained_only=False)
if not history:
    st.info("No deviations recorded yet.")
else:
    history_rows = [
        {
            "ID":       d["id"],
            "Date":     d["check_date"],
            "Scenario": d["scenario_key"],
            "Ticker":   d["ticker"] or node_labels.get(d["node_id"], ""),
            "Mode":     d["mode"] or "",
            "Expected": d["expected_outcome"],
            "Actual":   d["actual_summary"],
            "Reason":   d["reason"] or "(unexplained)",
            "By":       "🤖 system (auto)" if d["reason_by"] == "system" else (d["reason_by"] or ""),
            "Explained at": d["reason_ts"] or "",
        }
        for d in history
    ]
    st.dataframe(pd.DataFrame(history_rows), hide_index=True, height=35 * (min(len(history_rows), 15) + 1) + 10)

st.divider()

# --- Section 2: today's scenario status ---
st.header("Today's Scenarios")
today = date.today().isoformat()
# scenario_key alone is not a unique key -- two active scenario_expectations
# rows can share one scenario_key when disambiguated by node_id/mode (e.g. the
# same designed scenario run against two different nodes on purpose); keying
# by scenario_key alone would let one row's deviation mask the other's (found
# by Opus review, 2026-07-25).
today_deviations = {
    (d["scenario_key"], d["ticker"] or "", d["node_id"], d["mode"] or ""): d
    for d in load_deviations() if d["check_date"] == today
}
expectations = load_scenario_expectations()

rows = []
for s in expectations:
    key = (s["scenario_key"], s["ticker"] or "", s["node_id"], s["mode"] or "")
    dev = today_deviations.get(key)
    if s["expected_frequency"] not in ("daily", "informational"):
        # coverage_check.py's run_check only evaluates 'daily'/'informational'
        # scenarios -- rendering others as "met" (just because no deviation
        # row exists) would be a guess, not an observation (found by Opus
        # review, 2026-07-25).
        status = "— not daily-checked"
        actual = reason = ""
    elif s["check_method"] not in CHECKERS:
        status = "?  unknown check_method"
        actual = reason = ""
    elif s["expected_frequency"] == "informational" and dev is None:
        # 'informational' rows ARE checked every day (2026-07-30), but a miss
        # deliberately never records a coverage_deviations row (no ticket) --
        # so unlike 'daily' rows, "no deviation row" here is NOT evidence of
        # "met." Rendering this as green "met" would be a guess dressed up as
        # an observation, the exact failure mode the branch above already
        # guards against for non-daily rows -- found by Opus review, 2026-07-30
        # (this branch previously fell through to "✓ met").
        status = "ℹ️  informational (checked daily, no ticket on miss -- see EOD log)"
        actual = reason = ""
    elif dev is None:
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
