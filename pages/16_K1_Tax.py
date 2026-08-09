"""K-1 / Section 1256 Tax Forecast dashboard -- planning aid only. Every number here
is indicative until the real K-1 arrives; not a substitute for CPA-prepared numbers.
Mirrors 14_Coverage.py's pattern: reads/writes k1_tax.py's own sqlite db directly."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import k1_tax

st.set_page_config(page_title="K-1 Tax Forecast", layout="wide")
st.title("K-1 / Section 1256 Tax Forecast")
st.caption("Indicative planning aid only — confirm against the real K-1 when received. "
           "Not a substitute for CPA-prepared numbers.")

k1_tax.ensure_tables()

year = st.sidebar.number_input("Tax year", min_value=2020, max_value=2100, value=date.today().year, step=1)
buffer_quarters = st.sidebar.slider("Bucket B buffer (quarters of step-up covered)", 0, 4, 4)

st.sidebar.divider()
st.sidebar.subheader("Log a trade")
with st.sidebar.form("trade_form", clear_on_submit=True):
    t_date = st.date_input("Date", value=date.today())
    t_ptp = st.text_input("PTP / ticker", value="AGQ")
    t_gain = st.number_input("Realized gain/loss ($)", value=0.0, step=100.0)
    t_note = st.text_input("Note", value="")
    if st.form_submit_button("Log trade"):
        k1_tax.add_trade(t_date, t_ptp.strip().upper(), t_gain, t_note)
        st.sidebar.success(f"Logged {t_ptp} {t_gain:+.2f}")
        st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Rate config")
rates = k1_tax.load_rate_config()
with st.sidebar.form("rate_form"):
    fed_ord = st.number_input("Federal ordinary rate", value=rates.federal_ordinary_rate, format="%.4f")
    fed_lt = st.number_input("Federal LT cap gains rate", value=rates.federal_lt_rate, format="%.4f")
    niit = st.number_input("NIIT rate", value=rates.niit_rate, format="%.4f")
    state = st.number_input("State rate", value=rates.state_rate, format="%.4f")
    city = st.number_input("City rate", value=rates.city_rate, format="%.4f")
    lt_frac = st.number_input("Section 1256 LT fraction", value=rates.section_1256_lt_fraction, format="%.2f")
    st_frac = st.number_input("Section 1256 ST fraction", value=rates.section_1256_st_fraction, format="%.2f")
    if st.form_submit_button("Save rates"):
        k1_tax.save_rate_config(k1_tax.RateConfig(fed_ord, fed_lt, niit, state, city, lt_frac, st_frac))
        st.sidebar.success("Saved")
        st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Safe harbor / reserves")
with st.sidebar.form("liability_form"):
    ly = st.number_input("Prior-year liability year", min_value=2020, max_value=2100,
                          value=int(year) - 1, step=1)
    la = st.number_input("Total tax liability that year ($)", value=k1_tax.get_prior_year_liability(int(ly)) or 0.0)
    if st.form_submit_button("Save prior-year liability"):
        k1_tax.save_prior_year_liability(int(ly), la)
        st.sidebar.success("Saved")
        st.rerun()

with st.sidebar.form("reserve_form"):
    bucket_choice = st.selectbox("Bucket", ["A", "B"])
    bal = st.number_input("Current reserve balance ($)",
                           value=k1_tax.get_reserve_balance(bucket_choice))
    if st.form_submit_button("Set reserve balance"):
        k1_tax.set_reserve_balance(bucket_choice, bal)
        st.sidebar.success("Saved")
        st.rerun()

with st.sidebar.form("payment_form", clear_on_submit=True):
    p_date = st.date_input("Payment date", value=date.today(), key="p_date")
    p_amount = st.number_input("Payment amount ($)", value=0.0, step=500.0, key="p_amount")
    p_year = st.number_input("Tax year", min_value=2020, max_value=2100, value=int(year), step=1, key="p_year")
    if st.form_submit_button("Log payment"):
        k1_tax.add_payment(p_date, p_amount, int(p_year))
        st.sidebar.success("Logged")
        st.rerun()

# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

report = k1_tax.build_report(int(year), buffer_quarters=buffer_quarters)

st.info(report["note"])
st.metric("Blended effective rate", f"{report['blended_rate']:.2%}")

st.subheader("Per-PTP annual net gain")
st.caption("Silo'd per IRC §469(k) — a loss in one PTP never offsets another PTP's gain here.")
if report["per_ptp"]:
    df = pd.DataFrame([
        {"PTP": ptp, "Net gain": d["net_gain"], "Bucket A tax due": d["bucket_a_tax_due"],
         "Suspended loss carryforward": d["suspended_loss_carryforward"]}
        for ptp, d in report["per_ptp"].items()
    ])
    st.dataframe(df.style.format({"Net gain": "${:,.2f}", "Bucket A tax due": "${:,.2f}",
                                   "Suspended loss carryforward": "${:,.2f}"}),
                 use_container_width=True, hide_index=True)
else:
    st.write("No trades logged for this year yet.")

col1, col2 = st.columns(2)
for col, bucket_name, bucket in [(col1, "A — Tax due at filing", report["bucket_a"]),
                                  (col2, f"B — Safe-harbor step-up ({buffer_quarters}/4 quarters)", report["bucket_b"])]:
    with col:
        st.subheader(f"Bucket {bucket_name}")
        indicator = "🟢 Fully reserved" if bucket.fully_reserved else "🔴 Shortfall"
        st.write(indicator)
        m1, m2, m3 = st.columns(3)
        m1.metric("Needed", f"${bucket.needed:,.0f}")
        m2.metric("Reserved", f"${bucket.reserved:,.0f}")
        m3.metric("Shortfall", f"${bucket.shortfall:,.0f}")

st.subheader("Safe-harbor quarterly tracker")
if report["safe_harbor_schedule"]:
    sdf = pd.DataFrame([
        {"Quarter": q.label, "Due": q.due_date, "Required this Q": q.required_amount,
         "Cumulative required": q.cumulative_required, "Cumulative paid": q.cumulative_paid,
         "Behind?": "⚠ YES" if q.behind else "OK"}
        for q in report["safe_harbor_schedule"]
    ])
    st.dataframe(sdf.style.format({"Required this Q": "${:,.2f}", "Cumulative required": "${:,.2f}",
                                    "Cumulative paid": "${:,.2f}"}),
                 use_container_width=True, hide_index=True)
    nd = report["next_due"]
    if nd:
        days = (nd.due_date - date.today()).days
        st.write(f"**Next due:** {nd.label} on {nd.due_date} — ${nd.required_amount:,.2f} ({days} days away)")
else:
    st.write(f"No prior-year liability on file for {year} — set it in the sidebar to enable the tracker.")

st.subheader("Stress test: next quarter trading scenario")
delta = st.slider("Simulated next-quarter trading delta (%)", -100, 100, 0)
req_q = report["safe_harbor_schedule"][0].required_amount if report["safe_harbor_schedule"] else 0.0
st_result = k1_tax.stress_test_flat_quarter(report["bucket_b"].reserved, req_q, trading_delta_pct=delta)
st.write(f"Required quarterly payment is fixed (safe harbor is prior-year based) regardless of this scenario: "
         f"**${st_result['required_quarterly']:,.2f}**")
if st_result["covered_without_touching_capital"]:
    st.success(f"Bucket B reserve (${st_result['reserved_b']:,.2f}) covers the next required payment "
               f"without touching trading capital.")
else:
    st.error(f"Bucket B reserve (${st_result['reserved_b']:,.2f}) falls short by "
              f"${st_result['shortfall']:,.2f} — would need to draw from trading capital.")

st.subheader("Reserve yield accrual (optional)")
yc1, yc2, yc3 = st.columns(3)
yield_rate = yc1.number_input("Annual yield rate (e.g. T-bill)", value=0.045, format="%.4f")
days = yc2.number_input("Days held", value=90, step=1)
principal_bucket = yc3.selectbox("Bucket to project", ["A", "B"], key="yield_bucket")
principal = report["bucket_a"].reserved if principal_bucket == "A" else report["bucket_b"].reserved
accrued = k1_tax.reserve_with_yield(principal, yield_rate, int(days))
st.write(f"Simple-interest accrual on Bucket {principal_bucket}'s ${principal:,.2f} reserve over {days} days "
         f"at {yield_rate:.2%}/yr: **${accrued:,.2f}**")

st.subheader("Trade log")
trades = k1_tax.get_trades(year=int(year))
if trades:
    tdf = pd.DataFrame([{"Date": t.trade_date, "PTP": t.ptp, "Gain/Loss": t.gain, "Note": t.note}
                         for t in trades])
    st.dataframe(tdf.style.format({"Gain/Loss": "${:,.2f}"}), use_container_width=True, hide_index=True)
else:
    st.write("No trades logged for this year yet.")
