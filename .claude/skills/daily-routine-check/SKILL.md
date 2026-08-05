---
name: daily-routine-check
description: Validates the trading system's SOD (start-of-day)/intraday/EOD state -- live positions, canary scenario coverage, paper trading -- using the right existing scripts in the right order, with interpretation guidance for what a coverage-check "miss" actually means before treating it as a real problem. Use when the user asks to "check live," "do the daily routine," "is everything OK," or reacts to an alarming-looking EOD Slack report and wants to know if it's real.
---

# Daily routine check

Built 2026-08-04 (very late) after a long, ad hoc, partly-wrong investigation
into an EOD report that looked alarming but mostly wasn't -- this codifies
the right tools and the right order, and the specific traps that produced
wrong answers that night.

## Order of checks

1. **`scripts/daemon_status.py`** -- is the daemon actually running, or stale?
   If the user says they just took it down, that's expected, not a problem.

2. **`signals_invariants.py`** -- config-drift + invariant check (also prints
   `print_all_live_node_state()` per soxl_ira/ira). Catches: known invariant
   violations (e.g. AGQ's standing K-1/tax-exclusion flag, deliberately
   left as-is -- don't re-flag it as new), and whether every live node has a
   `staged_test_config` baseline row (a node flipped live without one has
   **zero** config-drift protection -- see
   `docs/watchlist_candidate_checklist.md` check 14).

3. **`scripts/status_check.py`** -- the one-stop live/paper position status
   tool: daemon health, config invariants, unexplained deviations, real
   broker + DB state per ticker, trigger distances. **Use this instead of ad
   hoc DB queries or `active_signals.log` grepping** -- both are traps (see
   below). **Known gap**: it reports `open_positions` only, not
   `pending_buys` -- a still-resting trailing-buy (entered a prior day, not
   yet bounce-filled) shows as misleadingly "flat" here. Cross-check
   `pending_buys` directly for any ticker that looks unexpectedly flat.

4. **`scripts/coverage_check.py`** -- canary + reconciliation deviation
   check. **Read the interpretation section below before treating any "no
   closed trade found" as a real problem.**

5. **`scripts/paper_vs_backtest_reconcile.py --tickers <paper-active tickers>`**
   -- does real paper-trading activity match what the corrected backtest
   predicts for the same window? Flags direction mismatches and trade-count
   gaps. Built 2026-08-04 (very late) -- the intent to validate paper against
   backtest existed almost two weeks before the script did (see
   `docs/research_log.md`), so don't let this one go stale/unused the same
   way. If it flags a divergence, drill into the specific ticker/signal with
   **`scripts/paper_signal_intrabar_check.py --ticker TICKER`** -- pulls
   paper's real captured `signal_price` (ground truth, not an estimate) and
   compares it against the hourly bar the backtest sees, to tell apart real
   intra-window price noise (a genuine detection gap in hourly-bar backtest
   resolution) from a bug in the backtest replay logic itself.

## Interpreting a coverage_check.py "miss" -- don't take the label at face value

A `trade_lifecycle` miss ("no closed trade found for TICKER") can mean
several different things, most of them not a bug. Check in this order
before concluding anything is actually wrong:

1. **Already open from a prior day?** Check `status_check.py`'s per-ticker
   output (or `open_positions` directly) -- if a position opened yesterday
   and hasn't resolved yet, "no closed trade *today*" is just describing an
   in-progress trade, not a miss.

2. **Still-resting pending buy from a prior day?** Check `pending_buys`
   directly (see status_check.py's gap above) -- a trailing-buy that hasn't
   bounce-filled yet is real, active, legitimate state, not nothing.

3. **Did the ticker's z-score actually cross its entry threshold today at
   all?** Recompute it directly from cached price data (same method as the
   research scripts: `prep_inputs`/`generate_daily_indicators`, or a minimal
   version -- see `scripts/z_entry_velocity_audit.py` for the pattern), not
   by grepping `logs/active_signals.log`. **That log has no reliable
   per-line date stamp** (only `[HH:MM:SS]`) and spans many days without a
   confirmed rotation boundary -- grepping it for "today's" activity can
   silently pull matches from a different day and produce a confidently
   wrong answer (this happened during the session that prompted this skill:
   a stale/mismatched log line was read as "today," giving a wrong
   explanation that had to be walked back).

4. Only after ruling out 1-3 should a miss be treated as a genuine,
   worth-investigating deviation.

## The EOD Slack report is deliberately strict -- long doesn't mean broken

The report follows a "no unexplained failure stays silent" ticket model
(`docs/deep_backlog.md`'s 2026-07-27 entry) -- every miss gets flagged and
demands a human-supplied reason via `--explain`, regardless of how mundane
the real cause is. A day with several canary tickers simply not crossing
their entry threshold (real, ordinary market behavior) will produce a long,
alarming-looking report by design. Don't read length or red-circle-emoji
count as a severity signal on its own -- check each line per the
interpretation steps above.

## Other traps from that session

- **Don't assume a printed status/log message reflects the real config.**
  `active_signals.py`'s own "outside signal window" message hardcodes a
  stale time string that doesn't match the real `_SIGNAL_WINDOWS` constant
  -- found live during this check. When in doubt, grep the actual constant
  in code, not a printed message about it.
- **Check `scripts/list_scripts.py --grep <keyword>` before writing any ad
  hoc query or script** -- most of what this routine needs already has a
  dedicated, tested script (`status_check.py`, `paper_trading_status.py`,
  `watchlist_status.py`, `coverage_matrix.py`, etc.).
- **Don't claim something "doesn't appear covered/tested" without checking
  `scripts/coverage_registry.py` first** -- it's the real ground truth for
  live/fake-venue proof status, and general impression is not reliable here.
