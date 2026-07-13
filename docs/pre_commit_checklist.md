# Pre-Commit Checklist

Used by `feature wrap` and `session wrap` before committing.

## Checks

- [ ] No secrets or API keys in staged files (check `.env` is gitignored)
- [ ] No runtime artifacts staged (`cache/`, `logs/`, `output/`, `active_phase_grid.json`, `current_test.json`)
- [ ] `docs/design.md` reflects any architectural changes made this session
- [ ] `docs/backlog_cache.md`/`docs/deep_backlog.md` updated if new issues or ideas surfaced
- [ ] `readme.md` updated if layer behavior changed
- [ ] Staged files reviewed — nothing unexpected included
- [ ] **If `active_signals.py`, `strategies.py`, or `backtester.py` changed this session**: run
      `.venv/bin/python scripts/verify_trailing_buy_resolution.py --tickers AGQ,SOXL` and
      `.venv/bin/python scripts/verify_trailing_sell_resolution.py --tickers AGQ,SOXL` — quick
      live-vs-backtest regression control for the actual live strategy family
      (`TrailingBothZScoreBreakout`; `verify_live_parity.py` doesn't cover this strategy's
      entry side, see its own docstring). Investigate any new/unexpected MISMATCH before
      committing — don't just rerun the full watchlist without `--tickers` unless something
      looks wrong, it's a slower yfinance-heavy sweep.

## Notes
- This list grows over time as real mistakes are caught — add to it when something slips through
