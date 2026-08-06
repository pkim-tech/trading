# Cliff-Safety / Neighbor-Query Correctness Checklist

Built 2026-08-07 after the same bug (a neighbor-safety SQL query silently failing to hold a real
parameter fixed) was independently found in two places the same session: a fresh ad hoc query
written that session, and the pre-existing `scripts/top_safe_nodes.py` tool. Both produced false
results (SOXL's real live core config looked catastrophically unsafe, worst_neighbor=-136.3%, when
the real answer — confirmed after the fix — is +653.1%, genuinely safe) for the identical reason:
the neighbor search didn't filter `trail_buy_pct`, so "neighbors" being compared against included
wildly different trail_buy_pct configs (1-30%) instead of holding the node's real value fixed.

## The checklist

Before trusting *any* cliff-safety / robustness / neighbor-search query result:

1. **List every real parameter column the strategy actually has** — for this project's real
   strategies, that's at minimum: `window`, `z_score_threshold`, `stop_loss`, `entry_timing`,
   `arm_sell_pct`/`take_profit` (strategy-dependent), `trail_buy_pct`, `trail_sell_pct`,
   `max_hold_hours`. Write the list out explicitly, don't rely on recalling it from memory mid-query.

2. **For each one, decide explicitly: is this axis being intentionally varied (a real neighbor
   step), or must it be held exactly fixed at the node's real value?** There is no third option. A
   parameter that's silently omitted from the WHERE clause is neither — it's left to vary freely
   across whatever range the underlying data happens to contain, which is the actual bug.

3. **Verify the fixed columns are in the query's WHERE clause with an exact-equality (or `IS`,
   for nullable columns) filter, not just the varied columns with a BETWEEN.** The failure mode
   specifically looks like: `WHERE arm_sell_pct BETWEEN ? AND ? AND trail_sell_pct BETWEEN ? AND ?`
   with no clause at all for `trail_buy_pct` — visually easy to miss because the query "looks
   complete" with several real conditions already present.

4. **Sanity-check the neighbor count.** If the neighbor search returns dramatically more rows than
   the real grid step count would suggest (e.g. `CLIFF_RADIUS=2` on one axis should bound the
   neighbor count to a small, predictable number), that's a sign an axis is unconstrained. The
   SOXL bug's tell: 261 "neighbors" for a search meant to check a handful of nearby cells.

5. **Cross-check a known point.** Before trusting a "not safe" result on something that was
   *expected* to be safe (e.g. a node already deployed with real capital, or already validated by
   a different method), pull the exact base-config row directly and confirm the query's own
   "current config" row matches — if it doesn't (as it didn't here: the flagged "current" row's
   alpha didn't match the independently-confirmed real value), the query has a real bug, not the
   underlying data a real problem.

## Why this matters more than a normal bug

This isn't just "a bug was found and fixed" — it produced a **confident, specific, wrong claim**
("your SOXL config is fragile, no safe alternative exists anywhere") that could have driven a real
decision (downgrading a genuinely good config) before the same session caught its own error on a
second pass. Treat any cliff-safety/neighbor-search result that looks *surprising* (either
alarmingly bad or suspiciously good) as a prompt to run this checklist before acting on it, not
after.
