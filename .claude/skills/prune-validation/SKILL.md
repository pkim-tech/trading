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

`scripts/full_db_prune_validate.py` implements steps 1, 3, 4, 5 (locate_best_node
side only, all tickers, not just one tranche) as of 2026-08-07. Steps 2
(independent cross-check) and 6 (row-count regression log) are not yet
folded into that script -- currently run `scripts/top_safe_nodes.py`
separately by hand for a spot-check. Extending the script to do all of this
automatically is open, not yet built.

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
