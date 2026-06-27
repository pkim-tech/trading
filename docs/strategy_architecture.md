# Strategy Architecture

## Core Insight

A **node** is a fully instantiated strategy with concrete parameter values. It is the atomic unit of the system — everything else exists to find good nodes, monitor them, and trade them.

A node is defined as:

```
(strategy, params: dict)
```

Where `params` is strategy-defined. The strategy declares what parameters it takes. The system operates on nodes generically without knowing the internals of any specific strategy.

**Ticker is a parameter, not a first-class field.** ZScoreBreakout happens to take a single `ticker` param. A pairs strategy would take `ticker_long` and `ticker_short`. The schema doesn't assume either.

---

## Strategy Contract

Each strategy class must declare:

```python
class ZScoreBreakout(BaseStrategy):
    PARAMS = [
        Param('ticker',         type=str),
        Param('window',         type=int,   min=5,   max=50),
        Param('take_profit',    type=int,   min=1,   max=30),
        Param('stop_loss',      type=int,   min=1,   max=30),
        Param('max_hold_hours', type=int,   min=7,   max=140, step=7),
    ]
```

From `PARAMS` the system can:
- Build the sweep grid automatically
- Generate topology axes (however many dimensions the strategy has)
- Validate a node's params before storing
- Render the correct UI controls for node selection

The strategy also implements:
- `entry_signal(params, data) -> bool` — is now a buy?
- `exit_signal(params, position, data) -> reason | None` — TP / SL / TIME / custom

Exit conditions are part of the strategy because a future strategy might have different exit rules (e.g. exit on signal reversal rather than fixed TP).

---

## Node Identity

A node is uniquely identified by `(strategy_name, params_json)` where `params_json` is a canonical (sorted-key) JSON string of the full params dict.

```json
{"max_hold_hours": 140, "stop_loss": 9, "take_profit": 28, "ticker": "AGQ", "window": 20}
```

This replaces the current hardcoded `(ticker, strategy, window, take_profit, stop_loss, max_hold_hours)` columns.

---

## DB Schema (target)

### `strategies`
| column        | type | notes                          |
|---------------|------|--------------------------------|
| name          | TEXT | PK — matches class name        |
| description   | TEXT |                                |
| version       | TEXT | strategy logic version         |

### `nodes`
| column         | type | notes                                      |
|----------------|------|--------------------------------------------|
| id             | TEXT | PK — sha256 of (strategy_name, params_json) |
| strategy_name  | TEXT | FK → strategies.name                       |
| params_json    | TEXT | canonical JSON, sorted keys                |
| sweep_version  | TEXT | platform/data integrity tag                |

### `backtest_results`
| column          | type  | notes               |
|-----------------|-------|---------------------|
| node_id         | TEXT  | FK → nodes.id       |
| sweep_version   | TEXT  |                     |
| trades          | INT   |                     |
| win_rate        | REAL  |                     |
| strategy_return | REAL  |                     |
| alpha_vs_spy    | REAL  |                     |
| asset_bh        | REAL  |                     |
| spy_bh          | REAL  |                     |
| run_timestamp   | TEXT  |                     |

### `watch_list`
| column      | type | notes                    |
|-------------|------|--------------------------|
| id          | INT  | PK                       |
| node_id     | TEXT | FK → nodes.id            |
| label       | TEXT |                          |
| added_at    | TEXT |                          |

### `open_positions`
| column       | type | notes                    |
|--------------|------|--------------------------|
| id           | INT  | PK                       |
| node_id      | TEXT | FK → nodes.id            |
| signal_price | REAL |                          |
| signal_time  | TEXT |                          |
| entry_price  | REAL |                          |
| entry_time   | TEXT |                          |

---

## Sweep

The sweep asks each strategy for its `PARAMS`, builds the grid from the declared ranges, and iterates over all combinations. No hardcoded axes.

```python
for strategy_cls in active_strategies:
    grid = build_grid(strategy_cls.PARAMS)
    for params in grid:
        node_id = make_node_id(strategy_cls.name, params)
        if not already_computed(node_id, sweep_version):
            result = run_backtest(strategy_cls, params)
            store_result(node_id, sweep_version, result)
```

---

## Topology / UI

The topology page asks the strategy how many dimensions it has and what they're called. For a 4-param strategy it renders a 4D scatter (3 axes + color). For a 5-param strategy it adds a slider for the 5th dimension.

Node selection in the UI writes a `(node_id, label)` row to `watch_list`.

---

## Active Signals

`active_signals.py` knows nothing about param shapes. It:
1. Loads watch list entries (node_id + label)
2. Resolves node_id → (strategy_cls, params)
3. Calls `strategy.entry_signal(params, data)` — BUY or HOLD
4. Calls `strategy.exit_signal(params, position, data)` — reason or None
5. Fires notifications and records positions

---

## Migration Path

Current state: `backtest_cache` has hardcoded columns for ZScoreBreakout's params. This works fine until a second strategy with different params is added.

Migration is only needed when adding a new strategy. At that point:
1. Create `nodes` and `backtest_results` tables
2. Migrate existing `backtest_cache` rows into them (params_json built from existing columns)
3. Drop `backtest_cache`

No urgency to migrate before then.
