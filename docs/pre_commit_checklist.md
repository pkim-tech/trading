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
- [ ] **If `active_signals.py`, any `signals_*.py` module, or any `schwab_*.py` module changed
      this session**: run `.venv/bin/python scripts/evening_status.py all` (the real evening
      account check-in — Part 1 log warnings, Part 2 real capital-at-stake node states, Part 3
      trades-vs-kernel + unexplained deviations, Part 4 tomorrow's readiness) against the real
      live DB and actually read the output, not just confirm it exits cleanly. This is the same
      real-render requirement as the nightly-Slack-report item below, but for the tool a human
      actually runs to sanity-check live state before/after a change — added 2026-08-16 after a
      session shipped real live-trading code changes without running it at all.
- [ ] **If `active_signals.py`, any `signals_*.py` module, or any `schwab_*.py` module changed
      this session**: explicitly check whether the change could affect the future execution of
      an order or position that's already in flight right now (a resting order, an open
      position, a pending buy, an armed trail state) — not just new orders placed after the
      change lands. A behavior change that's correct for fresh state can still misfire against
      state that was written under the old code/assumptions (e.g. a schema/field-meaning change,
      a changed threshold, a changed reconciliation path). Check real current state first
      (`scripts/open_positions_status.py`, `scripts/watchlist_status.py`, a direct query of
      `pending_buys`/`open_positions`) before concluding nothing is in flight — don't assume from
      memory. If something is in flight and could be affected, state explicitly what happens to
      it under the new code, not just that new trades will behave correctly.
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
