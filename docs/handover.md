# Handover Deck

Compact current-state summary. Overwrite this on each session wrap. Read this on `go` instead of scrolling session_cache.md.

---

## Sweep Status

- **Running**: tmux `sweep_v15_full` — z=[2.0, 2.5, 3.0], 357 tickers, ordered watchlist-first then v1.4 alpha desc
- **Currently on**: DPST (~50% through at session close)
- **Log**: `logs/sweep_v15_full.log`
- **ETA**: Overnight. Watchlist tickers first, then universe by alpha rank.

## DB State

| Ticker | z=2.0 | z=2.5 | z=3.0 | Notes |
|--------|-------|-------|-------|-------|
| AGQ    | 72k (v1.4 copy) | partial | partial | sweep in progress |
| DPST   | 54k | in progress | in progress | |
| EDC    | 54k | queued | queued | |
| FAS    | 0 → being swept | 54k ✓ | 54k ✓ (0 positive alpha) | |
| LABU   | 72k ✓ | queued | deleted+requeued | |
| CRMX   | 54k | queued | queued | |
| universe | 54k each (v1.4 copy) | queued | queued | |

Total rows: ~35.8M

## Critical Context

- **Backtester bug fixed this session**: Numba kernel had `sma - std * 2.0` hardcoded. All prior v1.5 z=2.5/3.0 data was wrong (identical to z=2.0). Deleted 108k corrupt rows. Overnight sweep is first correct run.
- **FAS**: Hurst=0.57 (momentum), ADF non-stationary, z=3.0 zero positive alpha, z=2.5 only 1.43× B&H. Candidate for watchlist removal.
- **LABU**: Best v1.5 z=2.0 node is SL=18 (not SL=9 on watchlist). Revisit params after z=2.5/3.0 lands.

## Watchlist

| Ticker | Params (v1.4) | Status |
|--------|--------------|--------|
| AGQ    | w=20 TP=28 SL=9 hold=140h | active |
| DPST   | w=10 TP=21 SL=12 hold=126h | active |
| EDC    | (check Winners) | active |
| FAS    | w=10 TP=25 SL=10 hold=133h | candidate for removal |
| LABU   | w=20 TP=28 SL=9 hold=140h → SL=18 likely better | active |
| CRMX   | TBD — no v1.5 results yet | pending |

No open positions.

## Pending Decisions

1. Remove FAS from watchlist? (Hurst + sweep results say yes)
2. LABU params: SL=9 → SL=18? Wait for z=2.5/3.0 before deciding
3. v1.6 grid design: every-3 integers `[3,6,9,...,30]` = 6k nodes/ticker/threshold (discuss before building)
4. Hurst + ADF as screener columns — batch compute across 357 tickers, add to `tickers` table

## Next Session Actions

1. Check sweep completion (`tail logs/sweep_v15_full.log`)
2. Open Winners page → v1.5 → compare best node per ticker per z threshold
3. Decide on FAS watchlist status
4. Revisit LABU params with fresh z=2.5/3.0 data
5. Plan Hurst/ADF screener batch job before building
