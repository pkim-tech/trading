# Research Notes

## Hurst / ADF as Entry Filters — 2026-06-29

**Verdict: not actionable.**

Tested via `pages/7_Hurst_Filter.py` (Hurst) and `pages/8_ADF_Filter.py` (ADF p-value) across all qualifying watchlist nodes.

Results:
- MO (momentum, H≥cutoff) helped 43/87 nodes vs MR (mean-reverting) 31/87 — weak, inconsistent
- Non-stationary filter (p≥cutoff) showed benefit on AGQ, DPST, EDC, FAS but not LABU
- Fixed-cutoff test on FAS showed cherry-picking — all fixed cutoffs worse than base

Root cause: at-entry regime is backward-looking. Hurst/ADF can't detect regime change fast enough to predict trade outcome. Slight lean toward momentum entries (H≥0.5) but sample sizes too small (18-24 trades per node) to be confident.

**Next direction:** SPY trend / VIX level as entry filter.

---

## Sweep Parameter Findings — v1.5 (2026-06-29)

- **z=3.0 at w=10**: maxes at 4 trades over 2 years — too rare to trade; z=2.0 is the real edge
- **w=30**: no qualifying nodes for non-single-stock tickers — trend drift kills mean reversion at that timescale
- **SVXY** (inverse VIX): 93% return, 3.6× B&H, 18 trades at w=20 z=2.5 — marginal, too volatile to add to watchlist
