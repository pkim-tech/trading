---
name: prune-validation
description: Mandatory validation gate before running prune_backtest_cache.py (or any operation that rewrites/shrinks backtest_cache islands). Snapshots each ticker's best node before and after via two independently-implemented tools, plus a kept-row-count regression check, before ever swapping the pruned result into place. Use whenever the user asks to prune backtest_cache, run prune_backtest_cache.py, or discusses shrinking/re-pruning the research DB. Triggers on "prune", "prune_backtest_cache", "shrink the db", "re-prune", "island-only".
---

# Prune validation

Built 2026-08-07 after `prune_backtest_cache.py`'s island-selection query was
missing a `trades > 0` filter on its per-group "winner" pick -- a trivial
zero-trade config (`robust_alpha = 0.0` by construction) silently outranked
a real losing node, so the real node's island got dropped and a worse
fallback node surfaced as "best" after pruning (caught live on SPXU, tranche
2 of the liquidity-screen backfill). This is the **third** instance this
week of the same bug shape in this codebase (see `take_profit`/NULL and
`entry_timing` -- both also silently-missing-filter bugs in cliff-safety/
best-node queries) -- a written checklist (`docs/cliff_safety_query_checklist.md`)
already existed for this bug *shape* and still didn't stop it, which is why
this is a skill (actively matched every turn) instead of another doc
(easy to not recall mid-task).

## The gate, in order

1. **Snapshot best node per ticker, PRE, via `scripts/locate_best_node.py`**
   against the live (unpruned or previously-pruned) DB -- one line per
   ticker, tiny output.
2. **Independent cross-check, PRE, via `scripts/top_safe_nodes.py`** (or
   another tool with genuinely different selection logic -- not just a
   second call to the same function). This matters because comparing a
   tool's output against itself pre/post only catches bugs in the *pruning*
   step -- it can't catch a bug baked into the comparison tool itself. Two
   independently-implemented tools agreeing is real evidence; one tool
   agreeing with itself is not.
3. **Extract** (`prune_backtest_cache.py --build`) -- writes a new file,
   never touches the live DB. Nothing to lose yet.
4. **Snapshot POST** via both tools again, against the newly-extracted file
   (not the live DB).
5. **Compare PRE vs POST on both tools, every ticker.** Any mismatch means
   real data changed under a ticker's "best" pick -- stop, do not swap, fix
   the underlying bug, and rerun from step 1 (idempotent).
6. **Row-count regression check**: track kept-row-count per ticker across
   prune runs (persist a small log; each prune recomputes from scratch and
   has no memory otherwise). A ticker pruned for the *first* time shrinking
   from full-grid to island-only is expected -- no alert. A ticker that was
   *already* pruned before, whose kept count is now *lower* than last time
   without a deliberate, stated change to the island-selection algorithm,
   is suspicious -- flag it, don't silently proceed.
7. **Only if every ticker matches on every check**: run `--swap`, then
   delete the superseded pre-swap copy once satisfied. Never delete the
   permanent pre-any-pruning archive (`trading_universe.db.pre_prune_20260802_013426`)
   regardless of how confident this gate's result looks -- see its own
   note in `docs/session_cache.md`/CLAUDE.md for why.

## Reference implementation

`scripts/full_db_prune_validate.py` was rewritten same-day after a paired
Opus review (independent-cold + contextual, with a rebuttal exchange) found
the first version was structurally unable to catch the bug it existed for:
it hardcoded `version="v5"` (most tickers had zero v5 rows, so their
comparison was a vacuous `None==None` "pass"), and even where it did check,
it compared exactly one row per ticker against a query that's *the same
predicate* as the prune's own winner selection -- mathematically guaranteed
to match regardless of correctness. The rewrite instead compares, per real
`(ticker, strategy, version, window, z, entry_timing)` group (11,183 of them,
not 59 rows), the exact set of rows the group's island should contain
(fingerprinted via row-count + an XOR-folded hash of identity columns)
against what's actually in the newly-built pruned file -- this is step 5
above, comprehensively, plus step 6 (kept-row-count regression log, though a
second paired review found this log's own comparison has a blind spot: it
only iterates groups present in the *current* run, so a group that vanishes
to zero rows entirely isn't flagged by the regression log specifically --
the separate, primary PRE/POST mismatch check in step 5 does still catch
this, since it compares the union of PRE and POST group keys). Step 2
(independent cross-check) is still not folded into the script -- run
`scripts/top_safe_nodes.py` separately by hand, and note it uses a
*different* selection metric (raw `alpha_vs_spy`, ranked) than the prune's
own winner (`robust_alpha`), so its chosen candidate within a group is often
NOT the prune's island center -- meaning its own cliff-safety neighbor check
can read more optimistic post-prune purely because fewer neighbor rows
survived pruning around whatever row it happened to pick. Confirmed live on
AGQ/DPST 2026-08-07 (worst_neighbor read safer post-prune despite the exact
same "best" node being selected). This is a standing, accepted limitation of
`top_safe_nodes.py` specifically, not something this validation gate closes.

**Two more real gaps found and fixed same day, both now baked into
`prune_backtest_cache.py` itself, not just the validator:**
- `cmd_build()` was only copying `sqlite_master type='table'` schema --
  indexes were silently dropped on every swap (confirmed: the 2026-08-07
  swap dropped `idx_bc_ticker`/`idx_bc_version_ticker_strategy`, invisible
  to the validator since it only checks row content, not schema/indexes;
  only surfaced afterward as query performance degrading). `cmd_build()` now
  creates the standard indexes on the pruned file directly (mirrors
  `run_optimization_sweep.rebuild_indexes()` -- keep the two lists in sync).
- `cmd_swap()`'s validation sentinel originally bound only to the *pruned
  file's* fingerprint, not the *live DB's* -- so a sweep writing new rows
  into the live DB during a long validation run (real same-day exposure:
  40+ minutes, spanning a lock-contention crash and an index rebuild) could
  have those new rows silently discarded on swap, with the sentinel still
  reading as valid. `write_validation_sentinel()`/`cmd_swap()` now also
  bind to `live_db_fingerprint()` (mtime+size of the live DB at validation
  time) and refuse to swap if the live DB has changed since. Note: the
  *normal* automated path (`run_liquidity_tranches.sh`'s tight
  sweep-then-immediately-prune sequencing, no other writer in between)
  doesn't hit this race in practice -- the exposure is specifically for
  ad-hoc/standalone validator runs, or multiple uncoordinated processes
  touching the same DB file (both true of parts of the 2026-08-07 session).

**Still open, not yet fixed:** the deterministic tiebreaker
(`TIEBREAK_SQL = "trades DESC, stop_loss, max_hold_hours"`) is real but
incomplete -- measured 2026-08-07: 5,514 of 11,183 real groups (49%) still
have a non-unique winner after this tiebreak, with ties spanning up to 9
units on the island-center axis (`take_profit`/`arm_sell_pct`, not in the
tiebreak). Didn't invalidate the 2026-08-07 swap itself (PRE/POST ran
against one static file in one process, so they agreed regardless), but
means island centers are still not fully reproducible across separate prune
runs. Fix: extend `TIEBREAK_SQL` to include the full remaining PK tuple
(`axis_tp`/take_profit-or-arm_sell_pct, `trail_buy_pct`, `trail_sell_pct`).

## Full-vs-partial island awareness (business-risk visibility, not a bug)

`scripts/top_safe_nodes_full_partial.py` (2026-08-07) reports each
candidate's `worst_neighbor` at radius=2 and radius=3, tagged FULL (a
genuinely complete, untruncated neighborhood exists) vs PARTIAL (truncated
by the *swept parameter grid's own real edge* -- e.g. `take_profit`/
`arm_sell_pct` topping out at 30.0 in the actual data, nothing to do with
pruning). Confirmed live: SOXL/AGQ/DPST's #1 candidates all sit at or next
to the grid's real maximum, so none have ever had a genuinely full
neighborhood at either radius, independent of any prune logic -- a
real, disclosed caveat for business risk judgment, not something to "fix" by
tightening the prune (extending the swept grid range is the actual lever,
and it's a resweep decision, not a prune-tooling one). Note this script's
own `is_full` check only reaches FULL when an axis has enough distinct
*values* on both sides -- verified 2026-08-07 that `stop_loss` has ≤3
distinct values database-wide for every strategy/version, so on the current
grid the FULL bucket is structurally unreachable at radius=3 for any
candidate; this is informational context for reading its output, not a bug
in the script.

## Never launch --swap or delete anything without a clean pass

Same posture as `backtest-change-rollout`'s "never launch the campaign
yourself" -- discussing/preparing this validation is not authorization to
run `--swap` or delete any file. Wait for explicit confirmation on the
actual destructive step, separate from agreeing the validation passed.

## WSL2 disk-space gotcha (recurred 2026-08-07, already known 2026-07-20)

`df -h`'s free-space number on a WSL2 virtual-disk mount does not reliably
reflect real available space on the Windows host backing it -- the vhdx can
report far more headroom than physically exists (confirmed 2026-07-20: `df`
said 826GB free, real available was 113GB; recurred 2026-08-07 with the
host's C: drive at 15G free while `df` inside WSL kept showing hundreds of
GB "avail"). Don't cite the `df` number as reassurance during a
disk-sensitive operation like this one -- ask the user for their real
available space instead of asserting a number they've already told you not
to trust.
