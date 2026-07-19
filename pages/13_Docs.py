import streamlit as st
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

st.set_page_config(layout="wide", page_title="Docs")
st.title("Docs")

DEFAULT_ORDER = [
    "research_log.md",
    "backlog_cache.md",
    "deep_backlog.md",
    "session_cache.md",
    "conversation_summary.md",
    "design.md",
    "watchlist_candidate_checklist.md",
    "pre_commit_checklist.md",
]


def list_docs():
    files = sorted(p.name for p in DOCS_DIR.glob("*.md"))
    ordered = [f for f in DEFAULT_ORDER if f in files]
    ordered += [f for f in files if f not in ordered]
    return ordered


docs = list_docs()
if not docs:
    st.info(f"No .md files found in {DOCS_DIR}")
    st.stop()

choice = st.sidebar.radio("Doc", docs)
path = DOCS_DIR / choice
text = path.read_text()

st.caption(str(path))
st.markdown(text)
