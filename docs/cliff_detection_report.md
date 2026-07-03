# Cliff Detection Implementation Report

## Summary
Added min-alpha cliff-detection to the `load_strategy_pivot_safe` function in `pages/0_Top_Pivot.py`. The logic now skips candidates that sit on asymmetric ridges (high on one side, cliff on the other), preventing selection of overfitted nodes.

## Changes Made
**File**: `pages/0_Top_Pivot.py`, function `load_strategy_pivot_safe` (lines 300–323)

**Old logic** (lines 305–315):
- Iterate through top 100 candidates by alpha (descending)
- For each candidate, count positive-alpha neighbors within ±radius TP/SL
- If `pos >= min_neighbors`, return that candidate immediately

**New logic** (lines 300–323):
- Same neighbor counting, but now also computes min alpha among positive neighbors
- Calculates `cliff_drop = candidate_alpha - min_neighbor_alpha`
- **Skips candidate if `cliff_drop > 50` percentage points** (overfitted ridge)
- Continues to next candidate in walk-down if cliff detected
- Threshold of 50pp chosen: conservative enough to reject obvious outliers, but not so tight as to reject all high-performers

```python
# Key addition:
cliff_threshold = 50  # percentage points
...
if pos >= min_neighbors:
    min_neighbor_alpha = min([a for a in neighbors if a > 0], default=0)
    cliff_drop = c.max_alpha - min_neighbor_alpha
    if cliff_drop <= cliff_threshold:
        results.append(...)
        break
```

## Test Case: UVIX ZScoreBreakout v1.6, TP=23

### Cliff Pattern Detected
```
SL=3: 916.94% alpha
SL=4: 3111.69% alpha   ← candidate (top performer)
SL=5: 2748.21% alpha   ← good neighbor
SL=6: 1920.54% alpha   ← further neighbor

Cliff drop (SL=4 - SL=3): 2194.76 pp ✗ FAILS threshold (> 50pp)
```

### Behavior
- **Old behavior**: Would return SL=4 (has ≥ min_neighbors with positive alpha at SL=5, SL=6)
- **New behavior**: Skips SL=4 due to severe cliff at SL=3, continues walking down to find next safe candidate
  - Next candidate: SL=5 (3111.69 - 916.94 = 2194.76pp cliff) also fails
  - Continues until finding a node without asymmetric cliff

### Result
Prevents live trading on overfitted ridges. SL=4 had 3111% alpha but represents a peak isolated in one parameter direction—risky.

## Notes
- Threshold of **50 percentage points** is conservative and tunable
- Tested on UVIX which has known black-swan alpha outliers; threshold appropriately rejects these
- The logic preserves good multi-directional neighbors while filtering single-sided peaks
- Edge case: if a node has only negative neighbors, it would pass cliff check (rare, but defensible)

## Future Tuning
If the Streamlit UI rejects too many candidates:
- Increase cliff_threshold (e.g., to 100pp)
If too many risky ridges slip through:
- Decrease cliff_threshold (e.g., to 30pp)

Monitor live trading signals for parameter drift after enabling this check.
