import streamlit as st
from pathlib import Path

st.set_page_config(layout="wide", page_title="Strategy")
st.title("Strategy Reference")

doc = Path("./docs/strategy.md").read_text()
st.markdown(doc)
