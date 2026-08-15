# Pre-Commit Checklist

Used by `feature wrap` and `session wrap` before committing.

## Checks

- [ ] No secrets or API keys in staged files (check `.env` is gitignored)
- [ ] No runtime artifacts staged (`cache/`, `logs/`, `output/`, `active_phase_grid.json`, `current_test.json`)
- [ ] `docs/design.md` reflects any architectural changes made this session
- [ ] `docs/backlog_cache.md`/`docs/deep_backlog.md` updated if new issues or ideas surfaced
- [ ] `.venv/bin/python scripts/check_backlog_cache_lean.py` — flags any `backlog_cache.md` entry
      over 2 lines (detection only; fix by relocating the full writeup to `deep_backlog.md` and
      leaving a one-line pointer, not by running a script unattended — see that script's docstring)
- [ ] `readme.md` updated if layer behavior changed
- [ ] Staged files reviewed — nothing unexpected included
- [ ] `.venv/bin/python signals_invariants.py` — config-invariant sanity check (currently: every
      `mode='live'` `TrailingExitZScoreBreakout` node is in `AUTOMATION_ENABLED_TICKERS`). Also
      runs automatically at daemon startup with a Slack alert; running it here catches a
      watchlist edit before the next restart.
- [ ] **If `active_signals.py`, `strategies.py`, or `backtester.py` changed this session**: run
      `.venv/bin/python scripts/verify_trailing_buy_resolution.py --tickers AGQ,SOXL` and
      `.venv/bin/python scripts/verify_trailing_sell_resolution.py --tickers AGQ,SOXL` — quick
      live-vs-backtest regression control for the actual live strategy family
      (`TrailingBothZScoreBreakout`; `verify_live_parity.py` doesn't cover this strategy's
      entry side, see its own docstring). Investigate any new/unexpected MISMATCH before
      committing — don't just rerun the full watchlist without `--tickers` unless something
      looks wrong, it's a slower yfinance-heavy sweep.
- [ ] **If any nightly Slack report's composition changed this session** (`scripts/
      coverage_report_summary.py`, `signals_notify.build_eod_scenario_review`/
      `build_tomorrow_plan`, or similar): actually render it against real data and read it —
      passing unit tests proves the code is internally consistent, not that a human reading it
      on a phone would find it clear (found 2026-08-15: a review-driven fix introduced two new
      inaccurate claims that no unit test caught because the tests asserted structure, not
      wording truthfulness). Render with `TRADING_DB_PATH=<real live db path>` and a stubbed
      `price_fn` for fast deterministic iteration, plus at least one run with every price
      forced to `None` to check the unpriced/degraded path specifically — that path is exactly
      where misleading claims hide, since it's the one nobody looks at day-to-day.

## Notes
- This list grows over time as real mistakes are caught — add to it when something slips through
