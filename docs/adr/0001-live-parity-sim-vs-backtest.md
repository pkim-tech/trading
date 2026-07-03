# 0001 — Live-parity test compares a live-code simulation against the backtest kernel

Status: Accepted (2026-07-03)

## Context

There are two independent implementations of the trading rules:

1. `backtester.py` — Numba-JIT kernels (`_simulate`, `_simulate_limit`, `_simulate_trail`,
   `_simulate_trail_buy`, `_simulate_trail_both`). Fast/vectorized, used for sweeps.
2. `strategies.py` — `check_signal`/`check_exit`, plain Python. Used live because the
   Numba kernels can't be called one-bar-at-a-time from a real-time polling loop.

`scripts/verify_live_parity.py` exists to catch drift between these two: it replays
`strategies.py` bar-by-bar (via its own loop) and diffs the resulting trades against
`backtester.py`'s kernel output.

`active_signals.py` is not a third implementation of the rules — it delegates every
BUY/SELL decision to `strategies.py`. What it does is orchestration: DB state
(`watch_list`/`open_positions`), Slack, and computing the *derived inputs* it hands to
`strategies.py` (`hours_held`, current price, low/high, which stop-loss % to use, etc.).

The 2026-07-03 P0 #1 bug (`hours_held` computed via wall-clock instead of bar-count)
lived entirely in this orchestration layer — `strategies.py`'s `check_exit` was always
correct. `verify_live_parity.py` didn't catch it because its replay loop computes
`hours_held` itself (correctly, via bar-index math) instead of calling
`active_signals.py`'s version — two independent implementations of the same derived
value, one of which was wrong, and the test only ever exercised the correct one.

Auditing `active_signals.py` for other derived values with the same risk surfaced:
- `check_sell_condition`: `hours_held` (fixed), `real_sl_pct`/`trail_pct` selection for
  v1.8/v1.9/v1.10 (fixed_sl strategies) — logic exists but has **zero** test coverage;
  `verify_live_parity.py`'s `kernel_trades()` doesn't even branch on v1.9/v1.10.
- `compute_buy_signal`: the `pd.Timestamp.now().normalize()` "exclude today" cutoff for
  daily indicators, the `low: current_price` proxy (no true intrabar low live), and the
  live-price-fetch-with-silent-fallback — all three are buy-side only and would be
  **completely unexercised** by a fix scoped to the exit side alone.

## Decision

Extend `verify_live_parity.py` rather than build a new script. Its `replay()` function
currently calls `strategies.py`'s `check_signal`/`check_exit` directly; change it to call
`active_signals.py`'s real `compute_buy_signal`/`check_sell_condition` instead. The
comparison stays structurally the same — sim trades vs. kernel (`backtester.py`) trades,
trade-by-trade, first-divergence reporting — only the source of the "sim" side changes,
from a proxy reimplementation to the actual live orchestration code.

Required supporting changes:
- `compute_buy_signal(node)` gets optional injectable params (`as_of`, `price_override`,
  `df_hourly_override`/`df_daily_override`), all defaulting to `None` = current live
  behavior unchanged. When supplied, used for historical replay with no look-ahead.
- `check_sell_condition` is already injectable (`now`, `df_hourly` are params) — no
  change needed.
- A throwaway SQLite DB is needed per test run, since `check_sell_condition` persists
  `trail_state` via `update_position_trail_state` (a real DB write) as a side effect.
- New v1.8/v1.9/v1.10 test cases added to `compare()`, and `kernel_trades()` extended
  with `run_backtest_v19`/`run_backtest_v110` branches (currently absent).

Both entry and exit sides are done together, not split — a bug in `compute_buy_signal`
means there's no signal to feed the (correctly verified) exit side at all, so partial
coverage of just one side was rejected as not meaningfully de-risking live trading.

## Note: why `kernel_trades()` recomputes fresh instead of reading `backtest_cache`

Considered comparing against a node's already-computed result in `backtest_cache`
instead of calling the kernel wrappers fresh. Rejected for two reasons: (1)
`backtest_cache` only stores aggregates (trade count, win_rate, return%, alpha), not
the trade-by-trade ledger needed for a real diff — a match on aggregates can hide
per-trade divergences that happen to cancel out; (2) a cached row is computed once and
can go stale if `backtester.py`'s kernel code changes afterward, which would make the
comparison mean "does live match some historical snapshot" instead of "does live match
the kernel as it exists right now" — the same staleness class P0 #2 fixed for
`fixed_sl` cache-hit lookups. `kernel_trades()` must keep calling `run_backtest*`
directly, not `backtest_cache`.

## Consequences

- Any future bug in `active_signals.py`'s derived-value computation (on either side) is
  caught automatically by comparison against the kernel, instead of requiring a human to
  notice a live/backtest behavioral mismatch after the fact.
- `verify_live_parity.py` becomes slower (real DB round-trips, not pure in-memory replay)
  and gains a DB dependency it didn't have before — acceptable since it's a manual/CI
  check, not a hot path.
- `compute_buy_signal`'s live-only behavior (real `yfinance` calls, real wall-clock
  "today") is preserved exactly when called with no injected params — zero risk to the
  production polling loop from this refactor.
- Known, accepted gaps that remain even after this change (not solvable by testing
  against the kernel, since they're live-only concerns with no backtest equivalent):
  the intrabar-low proxy on the buy side, and silent fallback to stale cached price if
  the live 1-minute fetch fails. These are design tradeoffs of live trading without true
  intrabar data, not bugs — flagged here so they aren't mistaken for solved.

## Update (2026-07-03, implementation)

- v1.9/v1.10 (`TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout`) were **not** added
  to `compare()` as originally planned. Auditing `active_signals.py` during implementation
  found it has zero live entry logic for the trailing-buy "wait for bounce above running
  low" state machine (tracked separately as P0 #3) — `compute_buy_signal` fires on raw
  z-score cross for every strategy, it doesn't implement the kernel's `_simulate_trail_buy`
  waiting state. Comparing them would just restate that known gap on every run rather than
  test derived-input correctness, which is what this ADR is actually about. `kernel_trades()`
  was still extended with `run_backtest_v19`/`run_backtest_v110` branches so the harness is
  ready once P0 #3 lands (test-first).
- Wiring the real `compute_buy_signal` into the replay immediately surfaced a much bigger,
  unplanned finding: the backtest kernel itself has look-ahead bias in entry-signal timing
  (every strategy's daily SMA/std includes that day's own not-yet-closed price — see
  `docs/backlog.md` "Look-ahead bias in every backtest's entry signal" for the full trace).
  This means the test currently reports MISMATCH on every case, including plain
  `ZScoreBreakout`, which has none of the other known gaps — that failure is expected
  and tracked, not a live bug. Until the kernel-side bias is fixed (separate, larger
  scope — needs a sweep rerun), this script's value is spotting *new* divergence
  (first-mismatch trade moving), not a clean pass/fail.
