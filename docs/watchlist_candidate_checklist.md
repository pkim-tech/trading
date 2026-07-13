# Watchlist Candidate Checklist

Run this on any ticker before promoting it to `live` (new candidate, or re-checking an
existing one after a macro/regime concern is raised). All checks use only cached hourly
data + yfinance 5-min bars — no broker/live data needed.

## 1. Macro/trend check
Is the underlying trending hard in one direction right now, independent of the backtest's
mean-reversion assumption?
```
.venv/bin/python -c "
import pandas as pd
df = pd.read_csv('cache/<TICKER>_1h.csv', index_col=0, parse_dates=True)
df.index = pd.to_datetime(df.index).tz_localize(None)
daily = df.resample('D').last().dropna()
print('30d return:', (daily['Close'].iloc[-1]/daily['Close'].iloc[-21]-1)*100)
print('90d return:', (daily['Close'].iloc[-1]/daily['Close'].iloc[-63]-1)*100)
"
```
A large sustained move (either direction) in the last 30-90d means every recent "buy the
dip" signal is fighting a real trend, not just chopping around a stable mean — worth
checking trade-level win-rate clustering (early vs. recent trades) before trusting the
full-history win rate as-is.

## 2. Trailing-buy resolution check (`TrailingBothZScoreBreakout` only)
`scripts/verify_trailing_buy_resolution.py` — re-detects every recent live-watchlist
bounce-entry signal using 5-min bars (real, continuous-ish tracking) and compares against
what the hourly-bar backtest kernel (`_simulate_trail_both`) would have caught. Run across
the whole active watchlist (live + research) in one shot:
```
.venv/bin/python scripts/verify_trailing_buy_resolution.py
```
Read the per-ticker summary at the bottom:
- **`mean` price diff** — how much worse (+) or better (-) a continuously-tracked fill
  would land vs. the backtest's hourly assumption. Within ~±0.5% is noise.
- **`median_intrahour_range_pct_of_trigger`** — the ticker's typical intra-hour High-Low
  swing divided by its `trail_buy_pct`. A ratio near/below 1 means the trigger is wide
  enough that hourly resolution barely matters. **Ratio > ~1.5-2 is a real flag** — the
  ticker is volatile enough relative to its own trigger that intra-hour swings can cause a
  premature/worse fill the hourly kernel doesn't model. (Confirmed 2026-07-12: SOXL at
  ratio 3.57 showed a real +1.81% mean fill-price penalty; TQQQ/NUGT in the 1.5-1.75 range
  showed +0.37-0.84%; everything under ~1.0 was within noise of 0%.)

If a candidate's ratio is high, either accept the known drift, or consider whether its
`trail_buy_pct` should be widened to better match its actual volatility.

## 3. Trailing-sell resolution check (`TrailingBothZScoreBreakout` only)
`scripts/verify_trailing_sell_resolution.py` — same idea as check 2, but for the exit
side: once the trailing-sell arms (price clears `arm_sell_pct`), re-detects the
peak/trail_stop crossing using 5-min bars and compares against what the hourly-bar
kernel's trailing branch (`_simulate_trail_both`) would have caught. Also run across the
whole active watchlist in one shot:
```
.venv/bin/python scripts/verify_trailing_sell_resolution.py
```
Same reading as check 2 (`mean` price diff, `median_intrahour_range_pct_of_trigger` —
here measured against `trail_sell_pct` instead of `trail_buy_pct`). Sign convention
differs from the buy check: negative mean diff means the 5-min exit fills *lower* (worse)
than the hourly kernel assumed. (Built 2026-07-13: all 11 watchlist tickers at parity,
mean diff -0.17% across 21 matched exits — unlike the buy side, live trailing-sell is
already monitored continuously by `active_signals.py` itself rather than handed off blind
to a broker order, so this check mainly validates the *backtest's* hourly-bar exit
modeling, not a live-execution gap. LABU showed -4.6% on a single sample — not enough
data yet to call it a real outlier, worth re-checking as more trades accumulate.)

**Note on both trailing-buy/sell resolution scripts**: `max_hold_hours` counts hourly
*bars*, not calendar hours (bars only exist ~7/trading day), so any cutoff-time math for
the 5-min replay must look up the real bar timestamp (`timestamps[entry_i + max_hold_hours]`)
rather than adding `timedelta(hours=max_hold_hours)` — the latter cuts the replay off
days early for longer holds and produces fabricated "ran out of data" results. Both
scripts had this bug until fixed 2026-07-13.

## 4. Win-rate stability check (train/live split)
Is the backtested win rate real, or an artifact of the older (training) portion of the
history — i.e., would a strategy that stopped working recently still show a good
full-history win rate? Replay the node's trades (same params as the live watchlist entry,
via `run_backtest_v110`/etc.), split chronologically 70/30, and compare:
```
tdf = <dataframe of replayed trades, oldest first>
n = len(tdf); cut = int(n * 0.7)
early, late = tdf.iloc[:cut], tdf.iloc[cut:]
print('early 70% win rate:', early['Result'].isin(['WIN','TWIN']).mean() * 100)
print('late 30% win rate:', late['Result'].isin(['WIN','TWIN']).mean() * 100)
```
A late win rate close to the early one means the edge isn't fading. Also eyeball the last
handful of trades directly (not just the aggregate) — a run of full stop-losses clustered
at the end is a real warning sign even if the aggregate late-window win rate still looks
fine (found for AGQ, 2026-07-12: 84% early vs. 81.8% late overall, but 2 of the last 4
trades were full -15% SL hits, both landing in the same recent downtrend window).

## 5. Live position hold-% / P&L check
For anything currently open, not just candidates: `python scripts/open_positions_status.py`
for entry price/time/shares, cross-referenced against the reference report's current
price and `hold=Xh/Yh` (hours held vs. `max_hold_hours`) and arm-% distance. Gives real
unrealized P&L per position and how close each is to arming/timing out — cheap gut-check
before deciding whether a "this looks bad" worry is about one ticker or the whole book
(found 2026-07-12: AGQ was the only red position out of four open; HIBL/EDC/SOXL were all
solidly positive).

## 6. Data-integrity check (stock splits)
`scripts/check_stock_splits.py` — queries yfinance's authoritative split history and flags
any split landing inside the ticker's cached date range. A missed split silently corrupts
the cached price series (huge fake gap/spike), producing phantom outlier trades and
inflated alpha. Caught real cases historically (UVIX, NBIZ). Cheap, run before trusting
any candidate's backtest numbers, not just at promotion time.

## 7. Fill-logic optimism check (`TrailingBothZScoreBreakout`/`TrailingBuyZScoreBreakout`)
`scripts/export_trades.py`'s `simulate_trail_both_ohlc_aware` — re-simulates entries
without assuming the best-case Low-before-High ordering within each hourly bar (the
standard kernel can't know which came first within an hour and picks the favorable
order). Quantifies how much a node's on-file return is overstated by that optimism.
Found historically to matter a lot for some tickers (SOXL's on-file return was ~2x
overstated, 7007%→3591%) — worth a spot-check on any candidate with an unusually strong
number before trusting it at face value.

## 8. Trade-count fluke check
Before trusting a "best alpha" node, check whether it's actually driven by a single
outlier trade (`trades` column at or near 1 for the winning grid cell) rather than a
real repeatable edge. Recurring failure mode in sweep results (e.g. UVIX had thousands of
`trades=1` rows driving misleadingly high headline alpha).

## Methodology notes (not standalone checks, but keep in mind while running the above)
- **Compare same node, not best-of-grid**, when checking whether a kernel/logic fix
  changed a ticker's numbers — re-optimizing across the whole grid after a fix confounds
  "did the fix help" with "did we find a different sweet spot."
- **Stop-loss width changes**: judge by total compounded return across all trades, not
  whether one specific losing trade would have survived a wider stop — a wider stop can
  look better on the trade that prompted the question while being worse in aggregate
  (more capital tied up, bigger average loss).
- **Already investigated and rejected**: Hurst exponent / ADF stationarity as entry
  filters (2026-06-28/29) — thorough research, concluded not actionable (lag problem,
  weak/inconsistent signal). Don't re-litigate without new information.

## When to run this
- Before flipping any ticker `research`→`live`.
- Whenever a live ticker's live behavior seems to be diverging from backtest expectations
  (the AGQ momentum discussion, 2026-07-12, is what prompted writing this down).
- Not needed on every session — this is a promotion/investigation gate, not a routine poll.
