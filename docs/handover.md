# Handover Deck

Compact current-state summary. Overwrite this on each session wrap. Read this on `go` instead of scrolling session_cache.md.

---

## Sweep Status

- **Restarted by user** after DB migration. Running in user's terminal (not tmux from Claude).
- **Command**: `python3 run_optimization_sweep.py 2>&1 | tee -a logs/sweep_v15_full.log`
- **Resume point**: AGQ had ~94k unvisited (z=2.5 partial + z=3.0 missing), then full universe z=2.5
- **Log**: `logs/sweep_v15_full.log`

## DB State (post-migration)

| Version | z=2.0 | z=2.5 | z=3.0 |
|---------|-------|-------|-------|
| v1.5    | 17.9M rows (filled from v1.4) | 36.6k (AGQ partial) | 9.0M (141 completed tickers) |

**Critical**: backtest_cache PRIMARY KEY bug was fixed this session. Old PK lacked `z_score_threshold`, causing z=2.5/3.0 INSERT OR REPLACE to silently overwrite z=2.0 rows. Table rebuilt with correct PK. v1.4 z=2.0 data copied as v1.5 z=2.0 (valid — bug only affected z≠2.0).

## Schema Changes This Session

- `watch_list` table: added `z_score_threshold REAL DEFAULT 2.0` column (migrated)
- `backtest_cache` PRIMARY KEY: now includes `z_score_threshold` (table rebuilt)

## Watchlist

| Ticker | Params (v1.4) | Status |
|--------|--------------|--------|
| AGQ    | w=20 TP=28 SL=9 hold=140h | active |
| DPST   | w=10 TP=21 SL=12 hold=126h | active |
| EDC    | (check Winners) | active |
| FAS    | w=10 TP=25 SL=10 hold=133h | candidate for removal |
| LABU   | w=20 TP=28 SL=9 hold=140h → SL=18 likely better | active |
| CRMX   | TBD | pending |

No open positions.

## UI Changes This Session

- **Winners page**: Return/Alpha/etc columns now sort numerically (was string). Z Thresh column added to watchlist section. Watchlist stats join now keyed on z_score_threshold.
- **Node Inspector** (pages/2_Node_Inspector.py): Full rebuild.
  - Watchlist at top (click to pre-fill node params)
  - Price chart with Bollinger bands at z=2.0, 2.5, 3.0 (4h downsampled for performance)
  - Trade entry/exit markers, win/loss shading
  - Rolling Hurst (30d window) subplot
  - Rolling ADF p-value (opt-in checkbox, cached after first run)
  - Hurst filter slider — drag H cutoff, see suppressed trades (grey circles) vs allowed trades; side-by-side metrics
  - Backtest, Hurst, ADF all cached by ticker+params — slider moves are instant

## Pending Decisions

1. Remove FAS from watchlist? (Hurst + sweep results say yes)
2. LABU params: SL=9 → SL=18? Wait for z=2.5/3.0 sweep results
3. v1.6 coarse grid design: every-3 integers `[3,6,9,...,30]` = 6k nodes/ticker/threshold
4. Hurst + ADF screener columns — batch compute across 357 tickers, add to `tickers` table
5. Portfolio backtest page (new) — replay all watchlist nodes simultaneously

## Next Session Actions

1. Check sweep progress (`tail logs/sweep_v15_full.log`)
2. Review Winners for z=2.5 results across watchlist tickers
3. Use Node Inspector H-filter slider on LABU, AGQ — calibrate H cutoff
4. Decide FAS watchlist removal
5. Revisit LABU SL=9 vs SL=18 with z=2.5/3.0 data
