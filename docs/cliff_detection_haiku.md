# Task: add min-alpha cliff-detection to cliff-safe walk-down

## Context
The cliff-safe logic in `pages/0_Top_Pivot.py` (`load_strategy_pivot_safe`, line ~60-110)
currently checks: "does this candidate have at least N positive-alpha neighbors within ±radius?"

But it's missing the real cliff-detection: checking for **asymmetric cliffs**. Example from
UVIX ZScoreBreakout v1.6 around TP=23:
- SL=4: 3111% alpha ← candidate
- SL=3: 0.22% alpha ← sharp cliff on one side (3108 percentage point drop)
- SL=5: 2748% alpha ← good neighbor on other side
- SL=6: 1920% alpha ← further neighbor

The current logic says SL=4 is "safe" because it has neighbors at SL=5 and SL=6 with
positive alpha. But it's actually a ridge (high on one side, cliff on the other), which
is risky for live trading — you're at an overfitted peak that's isolated in one direction.

## Goal
Add a second check to the walk-down: for each candidate node, find the **minimum alpha**
among all neighbors within ±radius (e.g., ±3 on both TP and SL). If the difference
between max alpha and min alpha is too large (e.g., > 50 percentage points? TBD after
we test), flag it as a cliff and skip it — keep looking for a better candidate.

## Implementation
- **File**: `pages/0_Top_Pivot.py`, function `load_strategy_pivot_safe` (lines ~60–110).
- **Current logic** (line ~85–105): iterates through candidates sorted by alpha desc,
  counts positive-alpha neighbors within ±radius. If `n >= min_neighbors`, breaks and
  returns that candidate.
- **New logic**: keep the neighbor-count check, but also add a min-alpha check. After
  counting positive neighbors, compute `min_alpha = min(alpha for (tp, sl, ..., alpha) in neighbors)`
  and `max_alpha - min_alpha`. If that diff > some threshold (propose 50%, but test and
  adjust), skip this candidate and keep iterating.

Example pseudocode:
```python
for r in candidates.itertuples():
    neighbors = [a for (tp, sl), a in gd.items()
                 if abs(tp - r.tp) <= radius and abs(sl - r.sl) <= radius
                 and a is not None and a > 0
                 and (tp, sl) != (r.tp, r.sl)]
    n = len(neighbors)
    if n >= min_neighbors:
        min_neighbor_alpha = min(neighbors) if neighbors else 0
        cliff_drop = r.max_alpha - min_neighbor_alpha
        if cliff_drop <= 50:  # TBD threshold
            # Safe candidate, return it
            return ...
        # else: skip, it's a cliff, keep looking
```

## Testing
- Use UVIX ZScoreBreakout as the test case (we know SL=4 should be rejected due to the
  SL=3 cliff, and SL=5 might also have the same issue).
- Run the walk-down and check which candidates it returns now. Document whether SL=4
  gets skipped and which SL is returned instead (probably SL=5 if it doesn't have a
  comparable cliff on the other side).
- For now, pick a reasonable cliff threshold (50 percentage points, or 50% drop) and
  document it in the code with a comment so it can be tuned later.

## Output
Write results to `docs/cliff_detection_report.md` (create it):
- What was changed (which lines in load_strategy_pivot_safe)
- Example output: run the walk-down on UVIX ZScoreBreakout, show before/after candidates
  (e.g., "old: returned SL=4, new: returned SL=5 because SL=4 has cliff at SL=3")
- Cliff threshold chosen and reasoning
- Any edge cases or surprises encountered

Keep it short — code diff + example result, not a full writeup.
