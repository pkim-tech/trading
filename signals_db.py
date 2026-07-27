"""
DB layer for active_signals: watchlists, watch_list nodes, open_positions,
pending_buys (trailing-buy lifecycle), and trade_log.
"""
import json
import sqlite3
import threading
from datetime import datetime

import strategies
import signals_config as cfg


def _conn():
    c = sqlite3.connect(cfg.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# Guards the check-then-act sections of open_position()/close_position()
# against a real intra-process race: the poll loop thread and the Slack
# Socket Mode handler thread (active_signals.py starts both) can both notice
# the same fill/exit and each pass their own SELECT-sees-nothing-yet check
# before either commits its INSERT/DELETE -- each connection's SELECT is a
# separate read that doesn't see the other's uncommitted write. A plain
# threading.Lock is enough (found via Opus review, 2026-07-22): this is a
# single-process, multi-thread daemon, not multiple processes, so this isn't
# the same cross-process concern schwab_safety._open_locked's file lock
# guards.
_position_lock = threading.Lock()


def ensure_tables():
    with _conn() as c:
        # watchlists table — named profiles, one is_active at a time
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("INSERT OR IGNORE INTO watchlists (name, is_active) VALUES ('main', 1)")
        if not c.execute("SELECT 1 FROM watchlists WHERE is_active=1").fetchone():
            c.execute("UPDATE watchlists SET is_active=1 WHERE name='main'")
        c.commit()

        main_id = c.execute("SELECT id FROM watchlists WHERE name='main'").fetchone()[0]

        # watch_list_audit: append-only log of watchlist/node mutations (create/delete/
        # activate a watchlist, add/remove/mode/label a node) — no audit trail existed
        # before 2026-07-18, discovered while trying to explain 47 deleted watchlists.
        c.execute("""
            CREATE TABLE IF NOT EXISTS watch_list_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL DEFAULT (datetime('now')),
                action       TEXT NOT NULL,
                watchlist_id INTEGER,
                watch_id     INTEGER,
                ticker       TEXT,
                detail       TEXT
            )
        """)
        c.commit()

        # watch_list: create fresh or migrate from old single-list schema
        wl_cols = {r[1] for r in c.execute("PRAGMA table_info(watch_list)").fetchall()}
        if not wl_cols:
            c.execute(f"""
                CREATE TABLE watch_list (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id      INTEGER NOT NULL DEFAULT {main_id} REFERENCES watchlists(id),
                    mode              TEXT NOT NULL DEFAULT 'live',
                    ticker            TEXT NOT NULL,
                    strategy          TEXT NOT NULL,
                    version           TEXT NOT NULL,
                    window            INTEGER NOT NULL,
                    take_profit       INTEGER,
                    stop_loss         INTEGER NOT NULL,
                    max_hold_hours    INTEGER NOT NULL,
                    z_score_threshold REAL NOT NULL DEFAULT 2.0,
                    label             TEXT DEFAULT '',
                    added_at          TEXT DEFAULT (datetime('now')),
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
                )
            """)
        elif 'watchlist_id' not in wl_cols:
            # migrate: recreate table with watchlist_id + mode + updated UNIQUE constraint
            c.executescript(f"""
                CREATE TABLE watch_list_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id      INTEGER NOT NULL DEFAULT {main_id},
                    mode              TEXT NOT NULL DEFAULT 'live',
                    ticker            TEXT NOT NULL,
                    strategy          TEXT NOT NULL,
                    version           TEXT NOT NULL,
                    window            INTEGER NOT NULL,
                    take_profit       INTEGER,
                    stop_loss         INTEGER NOT NULL,
                    max_hold_hours    INTEGER NOT NULL,
                    z_score_threshold REAL NOT NULL DEFAULT 2.0,
                    label             TEXT DEFAULT '',
                    added_at          TEXT DEFAULT (datetime('now')),
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
                );
                INSERT INTO watch_list_new
                    (watchlist_id, mode, ticker, strategy, version, window, take_profit, stop_loss,
                     max_hold_hours, z_score_threshold, label, added_at)
                SELECT {main_id}, 'live', ticker, strategy, version, window, take_profit, stop_loss,
                       max_hold_hours, COALESCE(z_score_threshold, 2.0), label, added_at
                FROM watch_list;
                DROP TABLE watch_list;
                ALTER TABLE watch_list_new RENAME TO watch_list;
            """)
        else:
            if 'mode' not in wl_cols:
                c.execute("ALTER TABLE watch_list ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'")
            if 'z_score_threshold' not in wl_cols:
                c.execute("ALTER TABLE watch_list ADD COLUMN z_score_threshold REAL NOT NULL DEFAULT 2.0")

        wl_cols = {r[1] for r in c.execute("PRAGMA table_info(watch_list)").fetchall()}
        if 'trail_sell_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_sell_pct REAL")
        if 'fixed_sl' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_buy_pct REAL")
        if 'arm_sell_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN arm_sell_pct REAL")
        if 'cached_avg_vol_10d' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN cached_avg_vol_10d REAL")
        if 'account' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN account TEXT")
        if 'alpha' not in wl_cols:
            # snapshot of backtest_cache.alpha_vs_spy at add_node/backfill time, not live-joined
            # (that DB is trading_universe.db, a separate file from this live DB) -- see
            # scripts/backfill_watch_list_alpha.py to (re)populate after adding/changing nodes.
            c.execute("ALTER TABLE watch_list ADD COLUMN alpha REAL")
        if 'entry_timing' not in wl_cols:
            # 'close' (default) checks at the existing bar-close signal windows only;
            # 'open_check' also gets an earlier poll near the bar's Open (see
            # active_signals._OPEN_CHECK_WINDOWS) -- mirrors backtest_cache.entry_timing.
            c.execute("ALTER TABLE watch_list ADD COLUMN entry_timing TEXT NOT NULL DEFAULT 'close'")
        if 'starting_notional' not in wl_cols:
            # Explicit per-ticker sizing floor, used by _last_sale_recovery only when
            # there's no closed-trade history yet -- backfilled to 50000 for every
            # existing row (the number every ticker was implicitly getting from the
            # old hardcoded fallback), now a real per-node value instead of a hidden
            # default so a new pilot (e.g. GDXD's $5k book) can be sized deliberately.
            c.execute("ALTER TABLE watch_list ADD COLUMN starting_notional REAL NOT NULL DEFAULT 50000")
        if 'annotation' not in wl_cols:
            # Freeform human note on why a node is in its current state (e.g. "walk-forward
            # clean, promoted 2026-07-18" / "excluded, negative fold, see backlog") -- distinct
            # from `label` (short display tag) and from watch_list_audit (mechanical mutation
            # log) -- this is the human-readable "why", not just the "what changed".
            c.execute("ALTER TABLE watch_list ADD COLUMN annotation TEXT")
        if 'paper_alert_verbose' not in wl_cols:
            # Default 0 (suppressed): paper trading is mostly for troubleshooting, not
            # routine review, so its Slack alerts are noise by default. Flip to 1 for a
            # ticker when actually weighing a go-live decision on it.
            c.execute("ALTER TABLE watch_list ADD COLUMN paper_alert_verbose INTEGER NOT NULL DEFAULT 0")

        # account wasn't part of the original UNIQUE constraint -- found 2026-07-26 while
        # adding a second real DPST node in a different account: two nodes with identical
        # strategy params but different accounts are genuinely distinct (the whole point of
        # the wl_id refactor), but the DB itself couldn't tell them apart and rejected the
        # insert outright. SQLite can't ALTER a UNIQUE constraint in place -- rebuild required.
        wl_schema_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watch_list'"
        ).fetchone()[0]
        if 'account' not in wl_schema_sql.split('UNIQUE(')[-1]:
            # DROP IF EXISTS first -- a prior failed rebuild attempt (e.g. a future
            # column mismatch between this CREATE and the live table) leaves this
            # orphaned, and CREATE TABLE with no IF NOT EXISTS would then permanently
            # brick every future ensure_tables() call (found by Opus review 2026-07-26).
            c.execute("DROP TABLE IF EXISTS watch_list_new")
            c.executescript("""
                CREATE TABLE watch_list_new (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id       INTEGER NOT NULL,
                    mode               TEXT NOT NULL DEFAULT 'live',
                    ticker             TEXT NOT NULL,
                    strategy           TEXT NOT NULL,
                    version            TEXT NOT NULL,
                    window             INTEGER NOT NULL,
                    take_profit        INTEGER,
                    stop_loss          INTEGER NOT NULL,
                    max_hold_hours     INTEGER NOT NULL,
                    z_score_threshold  REAL NOT NULL DEFAULT 2.0,
                    label              TEXT DEFAULT '',
                    added_at           TEXT DEFAULT (datetime('now')),
                    trail_sell_pct     REAL,
                    fixed_sl           REAL,
                    trail_buy_pct      REAL,
                    arm_sell_pct       REAL,
                    cached_avg_vol_10d REAL,
                    account            TEXT,
                    alpha              REAL,
                    entry_timing       TEXT NOT NULL DEFAULT 'close',
                    starting_notional  REAL NOT NULL DEFAULT 50000,
                    annotation         TEXT,
                    paper_alert_verbose INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit,
                           stop_loss, max_hold_hours, arm_sell_pct, trail_buy_pct,
                           trail_sell_pct, account)
                );
            """)
            # Copy every column watch_list actually has (not a hardcoded list) --
            # a hardcoded list here would silently drop any column added by the
            # ALTER ladder above within this same ensure_tables() call on a DB
            # that hasn't been rebuilt yet (found by Opus review 2026-07-26).
            live_cols = ', '.join(r[1] for r in c.execute("PRAGMA table_info(watch_list)"))
            c.execute(f"INSERT INTO watch_list_new ({live_cols}) SELECT {live_cols} FROM watch_list")
            c.execute("DROP TABLE watch_list")
            c.execute("ALTER TABLE watch_list_new RENAME TO watch_list")

        # open_positions
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                signal_price   REAL NOT NULL,
                signal_time    TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_time     TEXT NOT NULL,
                trade_log_id   INTEGER
            )
        """)
        op_cols = {r[1] for r in c.execute("PRAGMA table_info(open_positions)").fetchall()}
        if 'is_dry_run_sim' not in op_cols:
            # A dry_run account's real broker order is short-circuited before the API
            # call (schwab_client._place_trailing_order/_place_equity_order), so it
            # never generates a real fill event -- these positions are synthesized
            # against real price data instead (signals_notify.update_dry_run_buys)
            # and must be visibly distinguished from a genuine real/paper fill.
            c.execute("ALTER TABLE open_positions ADD COLUMN is_dry_run_sim INTEGER NOT NULL DEFAULT 0")
        if 'trade_log_id' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trade_log_id INTEGER")
        if 'trail_state' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_state TEXT")
        if 'trail_sell_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_sell_pct REAL")
        if 'fixed_sl' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_buy_pct REAL")
        if 'arm_sell_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN arm_sell_pct REAL")
        if 'shares' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN shares REAL")
        if 'account' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN account TEXT")
        if 'broker_stop_price' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN broker_stop_price REAL")
        if 'sl_order_id' not in op_cols:
            # Real broker order id for the resting STOP order placed on entry (Part 4,
            # Section 6) -- nullable since non-automated positions and legacy rows never
            # have one. Cancelled (and this cleared implicitly, the row goes away with the
            # arm transition) once the trailing-sell order takes over on TP arm.
            c.execute("ALTER TABLE open_positions ADD COLUMN sl_order_id INTEGER")
        if 'wl_id' not in op_cols:
            # The watch_list row's own PK -- the real per-node identity, since a ticker
            # alone (or (ticker, window)) is not unique once 2+ concurrent nodes exist for
            # the same ticker. Nullable: legacy rows opened before this migration are
            # backfilled best-effort below; new rows are always populated by open_position().
            c.execute("ALTER TABLE open_positions ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")

        # trade_log
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL,
                strategy            TEXT NOT NULL,
                version             TEXT NOT NULL,
                window              INTEGER NOT NULL,
                take_profit         INTEGER,
                stop_loss           INTEGER NOT NULL,
                max_hold_hours      INTEGER NOT NULL,
                signal_price        REAL NOT NULL,
                signal_time         TEXT NOT NULL,
                entry_price         REAL NOT NULL,
                entry_time          TEXT NOT NULL,
                entry_drift_pct     REAL NOT NULL,
                exit_signal_price   REAL,
                exit_price          REAL,
                exit_time           TEXT,
                exit_drift_pct      REAL,
                pnl_pct             REAL,
                exit_reason         TEXT,
                arm_sell_pct        REAL
            )
        """)
        tl_cols = {r[1] for r in c.execute("PRAGMA table_info(trade_log)").fetchall()}
        if 'arm_sell_pct' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN arm_sell_pct REAL")
        if 'shares' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN shares REAL")
        if 'account' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN account TEXT")
        if 'is_dry_run_sim' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN is_dry_run_sim INTEGER NOT NULL DEFAULT 0")

        # pending_buys -- tracks a trailing-buy order from BUY alert until Executed/Skipped
        # is confirmed, so a stalled broker-side fill can be reminded on (mirrors trail_state
        # on open_positions for the sell side, which has no equivalent pre-fill row to hang state off).
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_buys (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker            TEXT NOT NULL,
                node_json         TEXT NOT NULL,
                signal_price      REAL NOT NULL,
                signal_time       TEXT NOT NULL,
                order_placed      INTEGER NOT NULL DEFAULT 0,
                reminder_channel  TEXT,
                reminder_ts       TEXT,
                reminder_count    INTEGER NOT NULL DEFAULT 0,
                last_reminder_at  TEXT NOT NULL,
                created_at        TEXT NOT NULL
            )
        """)
        pb_cols = [r[1] for r in c.execute("PRAGMA table_info(pending_buys)")]
        if 'order_placed' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN order_placed INTEGER NOT NULL DEFAULT 0")
        if 'order_id' not in pb_cols:
            # Real broker order id (schwab.utils.Utils.extract_order_id), needed by
            # Part 3's overnight gap-correction check (signals_notify.check_gap_resize)
            # to cancel a still-resting automated trailing-buy order before replacing
            # it -- nullable since manual (non-automated) pending buys never have one.
            c.execute("ALTER TABLE pending_buys ADD COLUMN order_id INTEGER")
        if 'wl_id' not in pb_cols:
            # Same rationale as open_positions.wl_id -- promotes the watch_list PK (already
            # embedded in every row's node_json via _PENDING_BUY_NODE_KEYS) to a real,
            # queryable column so lookups/updates/deletes can key on it instead of ticker.
            c.execute("ALTER TABLE pending_buys ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")
        if 'running_low' not in pb_cols:
            # Only populated/read for a dry_run-account trailing buy (see
            # signals_notify.update_dry_run_buys) -- a real trailing-buy order's
            # running low is tracked by the broker itself, not this table.
            c.execute("ALTER TABLE pending_buys ADD COLUMN running_low REAL")
        if 'gap_resize_date' not in pb_cols:
            # Persisted idempotency guard for check_gap_resize -- a daemon restart
            # inside active_signals._GAP_CHECK_WINDOW resets the in-memory
            # gap_check_alerted set, which would otherwise let the same resting
            # order be cancelled/replaced twice (docs/backlog_cache.md, 2026-07-26).
            # Set once a row's gap condition is confirmed and acted on for the day,
            # checked before any cancel/replace attempt -- survives a restart.
            c.execute("ALTER TABLE pending_buys ADD COLUMN gap_resize_date TEXT")

        # paper_positions/paper_trade_log -- schema-identical mirrors of open_positions/
        # trade_log for schwab_safety.AUTOMATION_ENABLED_TICKERS tickers running in research
        # mode (see paper_trading.py). Never read/written by real order-placement code.
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                signal_price   REAL NOT NULL,
                signal_time    TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_time     TEXT NOT NULL,
                trade_log_id   INTEGER,
                trail_state    TEXT,
                trail_sell_pct REAL,
                fixed_sl       REAL,
                trail_buy_pct  REAL,
                arm_sell_pct   REAL,
                shares         REAL,
                account        TEXT,
                broker_stop_price REAL
            )
        """)
        pp_cols = {r[1] for r in c.execute("PRAGMA table_info(paper_positions)").fetchall()}
        if 'wl_id' not in pp_cols:
            c.execute("ALTER TABLE paper_positions ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")
        if 'is_dry_run_sim' not in pp_cols:
            # Always 0 here -- kept schema-identical with open_positions purely so
            # open_position()'s shared INSERT works unchanged against either table;
            # a paper position is never also a dry-run-sim one.
            c.execute("ALTER TABLE paper_positions ADD COLUMN is_dry_run_sim INTEGER NOT NULL DEFAULT 0")
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_trade_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL,
                strategy            TEXT NOT NULL,
                version             TEXT NOT NULL,
                window              INTEGER NOT NULL,
                take_profit         INTEGER,
                stop_loss           INTEGER NOT NULL,
                max_hold_hours      INTEGER NOT NULL,
                signal_price        REAL NOT NULL,
                signal_time         TEXT NOT NULL,
                entry_price         REAL NOT NULL,
                entry_time          TEXT NOT NULL,
                entry_drift_pct     REAL NOT NULL,
                exit_signal_price   REAL,
                exit_price          REAL,
                exit_time           TEXT,
                exit_drift_pct      REAL,
                pnl_pct             REAL,
                exit_reason         TEXT,
                arm_sell_pct        REAL,
                shares              REAL,
                account             TEXT
            )
        """)
        ptl_cols = {r[1] for r in c.execute("PRAGMA table_info(paper_trade_log)").fetchall()}
        if 'is_dry_run_sim' not in ptl_cols:
            # Always 0 -- see paper_positions.is_dry_run_sim above.
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN is_dry_run_sim INTEGER NOT NULL DEFAULT 0")
        # paper_pending_buys -- lighter than pending_buys: no reminder machinery, since a
        # simulated fill is auto-detected every poll (paper_trading.update_paper_buys),
        # never confirmed by a human clicking a button.
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_pending_buys (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker        TEXT NOT NULL,
                node_json     TEXT NOT NULL,
                signal_price  REAL NOT NULL,
                signal_time   TEXT NOT NULL,
                running_low   REAL NOT NULL,
                created_at    TEXT NOT NULL
            )
        """)
        ppb_cols = {r[1] for r in c.execute("PRAGMA table_info(paper_pending_buys)").fetchall()}
        if 'wl_id' not in ppb_cols:
            c.execute("ALTER TABLE paper_pending_buys ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")
        # open_price_quality_log -- Part 4 Deliverable 2: logs every pinned-check
        # get_session_open_price fetch (timestamp, ticker, target time, price,
        # is_true_open) so scripts/verify_open_price_quality.py can join it against
        # the real cached Open/Close the next day and confirm openPrice was populated
        # promptly, before flipping any ticker from paper to real order placement.
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_price_quality_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL DEFAULT (datetime('now')),
                ticker       TEXT NOT NULL,
                target_h     INTEGER NOT NULL,
                target_m     INTEGER NOT NULL,
                price        REAL NOT NULL,
                is_true_open INTEGER NOT NULL
            )
        """)
        c.commit()

        # coverage_events: one row per real firing of an automation control/phase
        # (SL placement, gap-resize, reconciliation, duplicate-order block, etc.),
        # tagged by which environment exercised it -- lets live_test_coverage.md's
        # scenario ledger (automation_principles.md #10) be answered by query
        # instead of hand-maintained status text.
        c.execute("""
            CREATE TABLE IF NOT EXISTS coverage_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL DEFAULT (datetime('now')),
                scenario_key  TEXT NOT NULL,
                mode          TEXT NOT NULL,
                ticker        TEXT,
                position_id   INTEGER,
                node_id       INTEGER REFERENCES watch_list(id),
                result        TEXT,
                detail        TEXT
            )
        """)
        c.commit()
        ce_cols = {r[1] for r in c.execute("PRAGMA table_info(coverage_events)").fetchall()}
        if 'node_id' not in ce_cols:
            c.execute("ALTER TABLE coverage_events ADD COLUMN node_id INTEGER REFERENCES watch_list(id)")
            c.commit()
        if 'strategy_type' not in ce_cols:
            # 3rd coverage axis (scenario_key x mode already existed) -- lets
            # "has the TrailingBoth SL-gating gap actually been exercised, in
            # which mode" be a real query. Not backfilled for existing rows
            # (their node_id may no longer resolve to a live watch_list row);
            # log_coverage_event derives it going forward from node_id when the
            # caller doesn't pass one explicitly.
            c.execute("ALTER TABLE coverage_events ADD COLUMN strategy_type TEXT")
            c.commit()

        # scenario_expectations: the "what should this node/control do" mapping,
        # structured instead of prose (was hand-maintained in deep_backlog.md's
        # canary writeup and live_test_coverage.md's table). check_method tells
        # coverage_check.py which real table to verify against -- 'coverage_event'
        # (a control-site scenario_key fired at all) or 'trade_lifecycle' (a real
        # trade_log/open_positions row shows the expected same-day entry/exit
        # shape) -- since coverage_events only logs control-site firings, not
        # entry/arm/exit for live/dry_run nodes (only paper_trading.py logs those).
        # node_id (FK to watch_list.id, nullable) is the real per-node identity key --
        # `ticker` alone is ambiguous (two distinct nodes can share a ticker, e.g. the
        # add_node dedup bug that shipped two TrailingBoth nodes differing only in
        # arm_sell_pct under the same ticker) and account-scoped, so a node_id-less
        # scenario means "applies at the ticker/account/global level, not one node".
        # `mode` (paper/dry_run/live, nullable = same expectation across all modes)
        # exists because the same scenario_key can legitimately behave differently per
        # environment (e.g. notional_cap is BUY-only in live/dry_run, meaningless in paper).
        # No UNIQUE constraint here on purpose -- see automation_principles.md #13 (never
        # rely on a UNIQUE constraint over a nullable column for dedup, the exact bug that
        # hit add_node twice). add_scenario_expectation() does an explicit COALESCE-based
        # check-then-upsert in Python instead.
        c.execute("""
            CREATE TABLE IF NOT EXISTS scenario_expectations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_key        TEXT NOT NULL,
                ticker              TEXT,
                node_id             INTEGER REFERENCES watch_list(id),
                mode                TEXT,
                strategy_type       TEXT,
                expected_outcome    TEXT NOT NULL,
                expected_frequency  TEXT NOT NULL,
                check_method        TEXT NOT NULL,
                check_params        TEXT,
                active              INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        c.commit()
        se_cols = {r[1] for r in c.execute("PRAGMA table_info(scenario_expectations)").fetchall()}
        if 'node_id' not in se_cols:
            # recreate without the old UNIQUE(scenario_key, ticker) -- it would reject a
            # second node sharing a ticker, exactly the identity gap node_id exists to close
            c.executescript("""
                CREATE TABLE scenario_expectations_new (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_key        TEXT NOT NULL,
                    ticker              TEXT,
                    node_id             INTEGER REFERENCES watch_list(id),
                    mode                TEXT,
                    strategy_type       TEXT,
                    expected_outcome    TEXT NOT NULL,
                    expected_frequency  TEXT NOT NULL,
                    check_method        TEXT NOT NULL,
                    check_params        TEXT,
                    active              INTEGER NOT NULL DEFAULT 1,
                    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO scenario_expectations_new
                    (id, scenario_key, ticker, strategy_type, expected_outcome,
                     expected_frequency, check_method, check_params, active, created_at, updated_at)
                SELECT id, scenario_key, NULLIF(ticker, ''), strategy_type, expected_outcome,
                       expected_frequency, check_method, check_params, active, created_at, created_at
                FROM scenario_expectations;
                DROP TABLE scenario_expectations;
                ALTER TABLE scenario_expectations_new RENAME TO scenario_expectations;
            """)
            c.commit()
        elif 'updated_at' not in se_cols:
            # SQLite rejects a non-constant (datetime('now')) default in ADD COLUMN
            # on a non-empty table ("Cannot add a column with non-constant
            # default") -- add nullable first, then backfill real rows explicitly,
            # matching created_at as a reasonable value for pre-existing rows
            # (this branch only runs on a DB that already has node_id but not
            # updated_at, i.e. an interrupted prior migration -- unreachable in
            # normal operation since both columns are added together in the
            # recreate branch above, but must not crash daemon startup if it ever is).
            # Deliberately nullable here (unlike the CREATE TABLE's NOT NULL DEFAULT) --
            # SQLite can't express the same NOT NULL+non-constant-default in an ALTER on
            # a non-empty table, and the immediate backfill below leaves no real row
            # NULL anyway, so this divergence from the canonical schema is harmless.
            c.execute("ALTER TABLE scenario_expectations ADD COLUMN updated_at TEXT")
            c.execute("UPDATE scenario_expectations SET updated_at = created_at WHERE updated_at IS NULL")
            c.commit()

        # coverage_deviations: one row per (check_date, scenario_key, node_id/ticker, mode)
        # where a daily expectation wasn't met. `reason` starts NULL --
        # unexplained -- until explain_deviation() fills it in. A row with
        # reason IS NULL is itself the actionable thing: an unexplained failure
        # is a bug by definition, not an acceptable end state (2026-07-24 reframe).
        # Same node_id/mode rationale and no-UNIQUE-on-nullable-columns rule as
        # scenario_expectations above.
        c.execute("""
            CREATE TABLE IF NOT EXISTS coverage_deviations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL DEFAULT (datetime('now')),
                check_date      TEXT NOT NULL,
                scenario_key    TEXT NOT NULL,
                ticker          TEXT,
                node_id         INTEGER REFERENCES watch_list(id),
                mode            TEXT,
                expected_outcome TEXT NOT NULL,
                actual_summary  TEXT NOT NULL,
                reason          TEXT,
                reason_by       TEXT,
                reason_ts       TEXT
            )
        """)
        c.commit()
        cd_cols = {r[1] for r in c.execute("PRAGMA table_info(coverage_deviations)").fetchall()}
        if 'node_id' not in cd_cols:
            c.executescript("""
                CREATE TABLE coverage_deviations_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              TEXT NOT NULL DEFAULT (datetime('now')),
                    check_date      TEXT NOT NULL,
                    scenario_key    TEXT NOT NULL,
                    ticker          TEXT,
                    node_id         INTEGER REFERENCES watch_list(id),
                    mode            TEXT,
                    expected_outcome TEXT NOT NULL,
                    actual_summary  TEXT NOT NULL,
                    reason          TEXT,
                    reason_by       TEXT,
                    reason_ts       TEXT
                );
                INSERT INTO coverage_deviations_new
                    (id, ts, check_date, scenario_key, ticker, expected_outcome,
                     actual_summary, reason, reason_by, reason_ts)
                SELECT id, ts, check_date, scenario_key, NULLIF(ticker, ''), expected_outcome,
                       actual_summary, reason, reason_by, reason_ts
                FROM coverage_deviations;
                DROP TABLE coverage_deviations;
                ALTER TABLE coverage_deviations_new RENAME TO coverage_deviations;
            """)
            c.commit()

        # trading_incidents: ticket-model log of real live-trading incidents
        # (a bug that actually fired against real order-placement code, not a
        # coverage gap or a "wired-never-fired" row) -- e.g. the 2026-07-26
        # phantom ERY fill on a non-trading day. Never deleted; resolved_ts
        # NULL means still open. Distinct from coverage_deviations (daily
        # scenario-expectation misses) and backlog_cache.md (planning/design
        # notes) -- this is specifically "something real and bad happened."
        c.execute("""
            CREATE TABLE IF NOT EXISTS trading_incidents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL DEFAULT (datetime('now')),
                title           TEXT NOT NULL,
                detail          TEXT NOT NULL,
                ticker          TEXT,
                account         TEXT,
                node_id         INTEGER REFERENCES watch_list(id),
                real_money_impact INTEGER NOT NULL DEFAULT 0,
                resolution      TEXT,
                resolved_by     TEXT,
                resolved_ts     TEXT
            )
        """)
        c.commit()

        # coverage_snoozes: a time-bounded, human-authored acknowledgment that a
        # known condition (e.g. UDOW's deliberately-seeded stale test position)
        # should stop generating both the coverage_events row and the Slack alert
        # for a scenario_key, scoped as narrowly or broadly as the caller wants via
        # nullable ticker/account/node_id (NULL = wildcard, matches any value).
        # Deliberately time-bounded (snoozed_until, not indefinite) -- unlike the
        # coverage_deviations ticket model (silence only ends when a human explains
        # it), a snooze re-alerts automatically on expiry so a silently-changed
        # underlying condition doesn't stay quiet forever just because someone
        # muted it once. is_snoozed() is the read path every alert/log call site
        # should check before firing.
        c.execute("""
            CREATE TABLE IF NOT EXISTS coverage_snoozes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_key   TEXT NOT NULL,
                ticker         TEXT,
                account        TEXT,
                node_id        INTEGER REFERENCES watch_list(id),
                kind           TEXT,
                snoozed_until  TEXT NOT NULL,
                reason         TEXT NOT NULL,
                snoozed_by     TEXT NOT NULL DEFAULT 'user',
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        c.commit()
        # kind (e.g. reconciliation_mismatch's "shares"/"missing_sl"/
        # "missing_trailing_sell") -- without this, a snooze scoped only to
        # scenario_key+ticker (the documented UDOW share-count-drift use case)
        # would also silently silence missing_sl/missing_trailing_sell for
        # that ticker, which mean "a real position may be unprotected at the
        # broker" -- a materially different, more severe class of alert than
        # the one actually being acknowledged. NULL = wildcard, same as the
        # other scope columns (found by session-wrap Opus review, 2026-07-28).
        cs_cols = {r[1] for r in c.execute("PRAGMA table_info(coverage_snoozes)").fetchall()}
        if 'kind' not in cs_cols:
            c.execute("ALTER TABLE coverage_snoozes ADD COLUMN kind TEXT")
            c.commit()

        # slack_message_log: full text of every real _post_message call (live,
        # sim, and webhook/socket alike) -- a message that scrolls past or gets
        # lost in Slack itself (e.g. the morning reference report) is otherwise
        # unrecoverable. Not a substitute for coverage_events (which tracks
        # control-site firing, not message content).
        c.execute("""
            CREATE TABLE IF NOT EXISTS slack_message_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL DEFAULT (datetime('now')),
                mode     TEXT NOT NULL,
                text     TEXT NOT NULL,
                error    TEXT
            )
        """)
        c.commit()
        # error column added 2026-07-22 -- the original schema logged intent
        # (this call was about to attempt a post) not delivery; a row's mere
        # existence was wrongly read as proof of a successful send during a
        # live missing-report incident. NULL = delivered/attempted with no
        # caught error, non-NULL = the exception/HTTP status _post_message caught.
        sml_cols = {r[1] for r in c.execute("PRAGMA table_info(slack_message_log)").fetchall()}
        if 'error' not in sml_cols:
            c.execute("ALTER TABLE slack_message_log ADD COLUMN error TEXT")
        c.commit()

        # wl_id backfill -- pending_buys/paper_pending_buys already embed node['id']
        # (the watch_list PK) inside node_json (_PENDING_BUY_NODE_KEYS), so this is a
        # deterministic parse-and-set, not a fuzzy match. Idempotent (only ever touches
        # still-NULL rows, cheap to re-run on every startup given this DB's size).
        for tbl in ('pending_buys', 'paper_pending_buys'):
            rows = c.execute(f"SELECT id, node_json FROM {tbl} WHERE wl_id IS NULL").fetchall()
            for r in rows:
                try:
                    node_id = json.loads(r['node_json']).get('id')
                except (TypeError, ValueError):
                    node_id = None
                if node_id is not None:
                    c.execute(f"UPDATE {tbl} SET wl_id=? WHERE id=?", (node_id, r['id']))
        c.commit()

        # open_positions/paper_positions carry no node_json to recover wl_id from --
        # best-effort match each still-NULL row to a current watch_list row on
        # (ticker, strategy, version, window, account). An unmatched/ambiguous row is
        # left NULL (acceptable: a legacy position from a now-changed/deleted node,
        # not a live one) -- every position opened via open_position() from here
        # forward always gets a real wl_id written at insert time.
        for tbl in ('open_positions', 'paper_positions'):
            rows = c.execute(
                f"SELECT id, ticker, strategy, version, window, account FROM {tbl} WHERE wl_id IS NULL"
            ).fetchall()
            for r in rows:
                candidates = c.execute(
                    "SELECT id FROM watch_list WHERE ticker=? AND strategy=? AND version=? AND window=? "
                    "AND COALESCE(account,'')=COALESCE(?,'')",
                    (r['ticker'], r['strategy'], r['version'], r['window'], r['account']),
                ).fetchall()
                if len(candidates) == 1:
                    c.execute(f"UPDATE {tbl} SET wl_id=? WHERE id=?", (candidates[0]['id'], r['id']))
        c.commit()


# ---------------------------------------------------------------------------
# Watch list CRUD
# ---------------------------------------------------------------------------

def _log_audit(c, action, watchlist_id=None, watch_id=None, ticker=None, detail=None):
    c.execute("""
        INSERT INTO watch_list_audit (action, watchlist_id, watch_id, ticker, detail)
        VALUES (?, ?, ?, ?, ?)
    """, (action, watchlist_id, watch_id, ticker, detail))


def get_watchlist_audit(limit=200):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list_audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


def get_watchlists():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watchlists ORDER BY name"
        ).fetchall()]


def get_active_watchlist_id():
    with _conn() as c:
        row = c.execute("SELECT id FROM watchlists WHERE is_active=1").fetchone()
        if row:
            return row[0]
        row = c.execute("SELECT id FROM watchlists ORDER BY id LIMIT 1").fetchone()
        return row[0] if row else None


def create_watchlist(name):
    """Idempotent by name -- returns the existing id if `name` is already taken,
    without attempting an insert. The prior implementation used INSERT OR IGNORE
    keyed on the UNIQUE name column: on a name conflict, SQLite still burns an
    AUTOINCREMENT id even though no row is written (confirmed 2026-07-20 -- the
    real watchlists.id gap from 57 to 65, 6 ids silently consumed with zero
    audit-log trace, was caused by exactly this). Checking existence first
    avoids the wasted-insert path entirely for the common case."""
    with _conn() as c:
        existing = c.execute("SELECT id FROM watchlists WHERE name = ?", (name,)).fetchone()
        if existing:
            return existing[0]
        cur = c.execute("INSERT INTO watchlists (name, is_active) VALUES (?, 0)", (name,))
        wl_id = cur.lastrowid
        _log_audit(c, 'create_watchlist', watchlist_id=wl_id, detail=name)
        c.commit()
        return wl_id


def delete_watchlist(watchlist_id):
    with _conn() as c:
        name_row = c.execute("SELECT name FROM watchlists WHERE id = ?", (watchlist_id,)).fetchone()
        node_count = c.execute(
            "SELECT COUNT(*) FROM watch_list WHERE watchlist_id = ?", (watchlist_id,)
        ).fetchone()[0]
        c.execute("DELETE FROM watch_list WHERE watchlist_id = ?", (watchlist_id,))
        c.execute("DELETE FROM watchlists WHERE id = ? AND is_active = 0", (watchlist_id,))
        _log_audit(c, 'delete_watchlist', watchlist_id=watchlist_id,
                   detail=f"name={name_row[0] if name_row else '?'} nodes_removed={node_count}")
        c.commit()


def set_active_watchlist(watchlist_id):
    with _conn() as c:
        prev = c.execute("SELECT id FROM watchlists WHERE is_active=1").fetchone()
        c.execute("UPDATE watchlists SET is_active = 0")
        c.execute("UPDATE watchlists SET is_active = 1 WHERE id = ?", (watchlist_id,))
        _log_audit(c, 'set_active_watchlist', watchlist_id=watchlist_id,
                   detail=f"prev_active={prev[0] if prev else None}")
        c.commit()


def get_watchlist(watchlist_id=None):
    with _conn() as c:
        if watchlist_id is None:
            watchlist_id = get_active_watchlist_id()
        if watchlist_id is None:
            return []
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE watchlist_id = ? ORDER BY ticker, id",
            (watchlist_id,)
        ).fetchall()]


def get_watch_list_node_by_id(node_id):
    """Real PK lookup -- unambiguous by construction, unlike get_watch_list_node's
    ticker-based best-effort matching. Returns None if node_id is None or the
    row no longer exists (e.g. a since-removed node)."""
    if node_id is None:
        return None
    try:
        with _conn() as c:
            row = c.execute("SELECT * FROM watch_list WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_watch_list_node(ticker, version=None, strategy=None, account=None, window=None,
                         watchlist_id=None):
    """Look up a single real watch_list row by ticker + optional disambiguators.
    Returns None if no match, more than one match (ambiguous -- caller should
    narrow with version/strategy/account/window rather than guess), or the
    lookup itself fails for any reason -- every current caller uses this only
    for coverage/observability enrichment (resolving a node_id to log
    alongside a real event), never as a gate on whether a real action
    proceeds, so this must never raise into that control flow (same
    fire-and-forget contract as log_coverage_event). Scoped to watchlist_id
    (defaults to the real active watchlist) -- without this, old watchlists
    (archived/superseded, e.g. watchlist 7's v3.26 nodes) can supply a
    same-ticker row that either falsely disambiguates a real live node to a
    stale one, or makes an otherwise-unique live node look ambiguous. Pass
    watchlist_id=False to search across all watchlists deliberately (e.g. a
    canary/test node not on the currently-active watchlist)."""
    try:
        if watchlist_id is None:
            watchlist_id = get_active_watchlist_id()
        q = "SELECT * FROM watch_list WHERE ticker = ?"
        params = [ticker]
        if watchlist_id:
            q += " AND watchlist_id = ?"
            params.append(watchlist_id)
        if version:
            q += " AND version = ?"
            params.append(version)
        if strategy:
            q += " AND strategy = ?"
            params.append(strategy)
        if account:
            q += " AND account = ?"
            params.append(account)
        if window is not None:
            q += " AND window = ?"
            params.append(window)
        with _conn() as c:
            rows = c.execute(q, params).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None
    except Exception:
        return None


def _config_fixed_stop_loss():
    try:
        with open(cfg.CONFIG_PATH) as f:
            return float(json.load(f).get("execution", {}).get("fixed_stop_loss", 0))
    except Exception:
        return 0.0


# TrailingBothZScoreBreakout's static per-run exit trail % for legacy v1.10/v2.10/v2.13-17
# nodes — this constant lived in config.execution.trail_pct at backfill time, not in any
# swept column, so it can't be recovered from the node's own row; hardcode the known mapping
# (see docs/design.md "Version Changelog"). v1.10/v2.10 ran at the default 3%.
_LEGACY_TRAILING_BOTH_TRAIL_PCT = {'v2.13': 1.0, 'v2.14': 2.0, 'v2.15': 3.0, 'v2.16': 4.0, 'v2.17': 5.0}


def _tp_or_arm_pct(row):
    """take_profit is a real take-profit % for most strategies, but for
    TrailingBothZScoreBreakout it's the arm-sell threshold, stored in arm_sell_pct
    instead (take_profit is NULL on those rows)."""
    if row['strategy'] == 'TrailingBothZScoreBreakout':
        return row['arm_sell_pct']
    return row['take_profit']


def _is_trailing_buy(node):
    buy_axis_col, _ = strategies.resolve_axis_columns(node['strategy'])
    return buy_axis_col == 'trail_buy_pct'


def add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
             label='', z_score_threshold=2.0, watchlist_id=None, mode='live',
             trail_buy_pct=None, trail_pct=None, entry_timing='close', starting_notional=50000,
             fixed_sl_override=None, account=None):
    """trail_buy_pct/trail_pct: pass the real values directly for v3.x nodes (where
    backtest_cache has real named columns). Omit both for legacy v1.x/v2.x nodes —
    falls back to reinterpreting stop_loss the way it's always meant for the 4
    trailing strategies (see docs/design.md 'Grid axis meaning by strategy').
    For v3.x trailing-both/trailing-exit nodes, the stop_loss arg is not a real
    swept value (backtest_cache stores config.execution.fixed_stop_loss there,
    a constant) — pass whatever backtest_cache's stop_loss column shows, it's vestigial.
    fixed_sl_override: pass the real per-node SL (e.g. a v4 SL-sweep value) directly —
    without it, uses_fixed_sl strategies always fall back to config.json's stale global
    default, which is wrong for any node whose real SL differs from that default."""
    if watchlist_id is None:
        watchlist_id = get_active_watchlist_id()
    if strategies.uses_fixed_sl(strategy):
        fixed_sl = fixed_sl_override if fixed_sl_override is not None else _config_fixed_stop_loss()
        if trail_buy_pct is None and trail_pct is None:
            sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy)
            if sl_axis_col == 'trail_buy_pct':
                stored_trail_buy_pct = float(stop_loss)
                stored_trail_sell_pct = (_LEGACY_TRAILING_BOTH_TRAIL_PCT.get(version, 3.0)
                                          if fourth_axis_col == 'trail_pct' else 0.0)
            else:
                stored_trail_buy_pct = 0.0
                stored_trail_sell_pct = float(stop_loss)
        else:
            # v3.x explicit pass — real values, validate against the strategy's schema.
            for w in strategies.validate_axis_values(strategy, trail_buy_pct, trail_pct):
                print(f"WARNING add_node({ticker}, {strategy}, {version}): {w}")
            stored_trail_sell_pct = trail_pct if trail_pct is not None else 0.0
            stored_trail_buy_pct = trail_buy_pct if trail_buy_pct is not None else 0.0
    else:
        # Strategy doesn't use trailing axes at all (e.g. bar-close ZScoreBreakout) —
        # flag if the caller passed either anyway, since it'll silently do nothing.
        for w in strategies.validate_axis_values(strategy, trail_buy_pct, trail_pct):
            print(f"WARNING add_node({ticker}, {strategy}, {version}): {w}")
        fixed_sl = None
        stored_trail_sell_pct = None
        stored_trail_buy_pct = None

    # take_profit is a real take-profit exit for most strategies, but for
    # TrailingBothZScoreBreakout it's actually the arm-sell threshold — store it
    # in arm_sell_pct instead so take_profit never means two different things.
    if strategy == 'TrailingBothZScoreBreakout':
        stored_take_profit = None
        stored_arm_sell_pct = float(take_profit)
    else:
        stored_take_profit = int(take_profit)
        stored_arm_sell_pct = None

    with _conn() as c:
        # Explicit check-then-skip instead of relying on the UNIQUE constraint +
        # INSERT OR IGNORE: SQLite never treats NULL == NULL as a conflict match,
        # and take_profit is genuinely NULL for TrailingBothZScoreBreakout nodes
        # (see docstring above), so INSERT OR IGNORE silently duplicates those
        # every rerun instead of no-op'ing. COALESCE normalizes NULL to a sentinel
        # so this check catches it regardless of column nullability.
        # arm_sell_pct/trail_buy_pct/trail_sell_pct are included even though
        # they were never part of the original UNIQUE constraint -- found by
        # Opus review 2026-07-24: for TrailingBothZScoreBreakout, take_profit
        # is always NULL and the real distinguishing value lives in
        # arm_sell_pct instead, so without it here two genuinely different
        # nodes (same take_profit=NULL, different arm_sell_pct) would now
        # silently collapse to one the moment the NULL-matching fix above
        # started actually enforcing the rest of the key -- a new silent-drop
        # bug introduced while fixing the old silent-duplicate one.
        # account is included too (2026-07-26) -- two nodes with otherwise
        # identical strategy params in *different* accounts are genuinely
        # distinct (the whole point of the wl_id refactor); only same-account
        # duplicates should be treated as real dedup hits.
        existing = c.execute("""
            SELECT id FROM watch_list
            WHERE watchlist_id = ? AND ticker = ? AND strategy = ? AND version = ?
              AND window = ? AND COALESCE(take_profit, -1) = COALESCE(?, -1)
              AND stop_loss = ? AND max_hold_hours = ?
              AND COALESCE(arm_sell_pct, -1) = COALESCE(?, -1)
              AND COALESCE(trail_buy_pct, -1) = COALESCE(?, -1)
              AND COALESCE(trail_sell_pct, -1) = COALESCE(?, -1)
              AND COALESCE(account, '') = COALESCE(?, '')
        """, (watchlist_id, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours),
              stored_arm_sell_pct, stored_trail_buy_pct, stored_trail_sell_pct, account)).fetchone()
        if existing:
            return
        cur = c.execute("""
            INSERT INTO watch_list
                (watchlist_id, mode, ticker, strategy, version, window, take_profit,
                 stop_loss, max_hold_hours, label, z_score_threshold, trail_sell_pct, fixed_sl,
                 trail_buy_pct, arm_sell_pct, entry_timing, starting_notional, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watchlist_id, mode, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours), label, float(z_score_threshold),
              stored_trail_sell_pct, fixed_sl, stored_trail_buy_pct, stored_arm_sell_pct,
              entry_timing, float(starting_notional), account))
        _log_audit(c, 'add_node', watchlist_id=watchlist_id, watch_id=cur.lastrowid,
                   ticker=ticker, detail=f"strategy={strategy} version={version} mode={mode}")
        c.commit()


def remove_node(watch_id):
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker, strategy, version FROM watch_list WHERE id = ?",
            (watch_id,)
        ).fetchone()
        c.execute("DELETE FROM watch_list WHERE id = ?", (watch_id,))
        if row:
            _log_audit(c, 'remove_node', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=f"strategy={row['strategy']} version={row['version']}")
        c.commit()


def set_node_mode(watch_id, mode):
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker, mode FROM watch_list WHERE id = ?", (watch_id,)
        ).fetchone()
        c.execute("UPDATE watch_list SET mode = ? WHERE id = ?", (mode, watch_id))
        if row:
            _log_audit(c, 'set_node_mode', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=f"{row['mode']} -> {mode}")
        c.commit()


def label_node(watch_id, label):
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker FROM watch_list WHERE id = ?", (watch_id,)
        ).fetchone()
        c.execute("UPDATE watch_list SET label = ? WHERE id = ?", (label, watch_id))
        if row:
            _log_audit(c, 'label_node', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=label)
        c.commit()


def annotate_node(watch_id, annotation):
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker FROM watch_list WHERE id = ?", (watch_id,)
        ).fetchone()
        c.execute("UPDATE watch_list SET annotation = ? WHERE id = ?", (annotation, watch_id))
        if row:
            _log_audit(c, 'annotate_node', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=annotation)
        c.commit()


def set_starting_notional(watch_id, starting_notional):
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker, starting_notional FROM watch_list WHERE id = ?", (watch_id,)
        ).fetchone()
        c.execute("UPDATE watch_list SET starting_notional = ? WHERE id = ?",
                   (float(starting_notional), watch_id))
        if row:
            _log_audit(c, 'set_starting_notional', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=f"{row['starting_notional']} -> {starting_notional}")
        c.commit()


def log_automation_scope_change(old_tickers, new_tickers):
    """Records a change to schwab_safety.AUTOMATION_ENABLED_TICKERS in the same
    append-only audit log used for watchlist/node mutations. Needed because that
    set moved from a hardcoded Python literal (git-tracked, self-documenting) to
    an .env var (gitignored, no history) -- this is now the only record of when
    the automation pilot scope changed and to what."""
    with _conn() as c:
        _log_audit(c, 'automation_scope_change', detail=f"{sorted(old_tickers)} -> {sorted(new_tickers)}")
        c.commit()


def log_coverage_event(scenario_key, mode, ticker=None, position_id=None, node_id=None,
                        result='', detail='', strategy_type=None):
    """Records one real firing of an automation control/phase, tagged by which
    environment exercised it. `mode` is one of 'paper'/'dry_run'/'live' -- the
    caller determines this from its own context (e.g. paper_trading.py always
    passes 'paper'; schwab_safety/signals_notify pass 'live' or 'dry_run' based
    on the account's real dry_run flag), not inferred here. node_id is the real
    watch_list.id identity key when the caller can resolve one (ticker alone is
    ambiguous -- two distinct nodes can share a ticker). strategy_type (the 3rd
    coverage axis, e.g. 'TrailingBothZScoreBreakout') is derived from node_id's
    real watch_list row when the caller doesn't pass one explicitly -- existing
    call sites don't need to change. Fire-and-forget: never raises past a
    logging failure into the caller's real control flow."""
    try:
        if strategy_type is None and node_id is not None:
            with _conn() as c:
                row = c.execute("SELECT strategy FROM watch_list WHERE id = ?", (node_id,)).fetchone()
            strategy_type = row['strategy'] if row else None
        with _conn() as c:
            c.execute("""
                INSERT INTO coverage_events (scenario_key, mode, ticker, position_id, node_id, result, detail, strategy_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (scenario_key, mode, ticker, position_id, node_id, result, detail, strategy_type))
            c.commit()
    except Exception:
        pass


def get_coverage_events(scenario_key=None, mode=None, strategy_type=None, limit=500):
    q = "SELECT * FROM coverage_events"
    clauses, params = [], []
    if scenario_key:
        clauses.append("scenario_key = ?")
        params.append(scenario_key)
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if strategy_type:
        clauses.append("strategy_type = ?")
        params.append(strategy_type)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def add_scenario_expectation(scenario_key, expected_outcome, expected_frequency, check_method,
                              ticker=None, node_id=None, mode=None, strategy_type=None, check_params=None):
    """Insert-or-update one row of the structured designed-scenario mapping.
    expected_frequency: 'daily' / 'occasional' / 'regression-only'.
    check_method: 'coverage_event' (verify via coverage_events scenario_key) or
    'trade_lifecycle' (verify via a real trade_log/open_positions row today).
    check_params is a free-form JSON string interpreted by coverage_check.py
    according to check_method (e.g. {"exit_reason": "TIME"} for trade_lifecycle).
    node_id is the real watch_list.id identity key (nullable -- a scenario that
    applies at the ticker/account/global level, not one node, leaves it None).
    mode is 'paper'/'dry_run'/'live' (nullable -- None means the expectation is
    the same across all modes). Dedup is on (scenario_key, node_id, ticker, mode)
    via an explicit COALESCE-based check-then-upsert, not a UNIQUE constraint --
    see automation_principles.md #13 (a raw UNIQUE over nullable columns silently
    fails to match NULL==NULL, the exact bug that hit add_node twice)."""
    with _conn() as c:
        existing = c.execute("""
            SELECT id FROM scenario_expectations
            WHERE scenario_key = ?
              AND COALESCE(node_id, -1) = COALESCE(?, -1)
              AND COALESCE(ticker, '') = COALESCE(?, '')
              AND COALESCE(mode, '') = COALESCE(?, '')
        """, (scenario_key, node_id, ticker, mode)).fetchone()
        if existing:
            c.execute("""
                UPDATE scenario_expectations SET
                    strategy_type=?, expected_outcome=?, expected_frequency=?,
                    check_method=?, check_params=?, active=1, updated_at=datetime('now')
                WHERE id=?
            """, (strategy_type, expected_outcome, expected_frequency, check_method, check_params, existing[0]))
        else:
            c.execute("""
                INSERT INTO scenario_expectations
                    (scenario_key, ticker, node_id, mode, strategy_type, expected_outcome,
                     expected_frequency, check_method, check_params)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (scenario_key, ticker, node_id, mode, strategy_type, expected_outcome,
                  expected_frequency, check_method, check_params))
        c.commit()


def get_scenario_expectations(expected_frequency=None, active_only=True):
    q = "SELECT * FROM scenario_expectations"
    clauses, params = [], []
    if expected_frequency:
        clauses.append("expected_frequency = ?")
        params.append(expected_frequency)
    if active_only:
        clauses.append("active = 1")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def record_deviation(check_date, scenario_key, expected_outcome, actual_summary, ticker=None,
                      node_id=None, mode=None):
    """Upsert a deviation row for this (check_date, scenario_key, node_id, ticker, mode).
    Leaves a HUMAN-authored reason in place if the row already exists (re-running the
    daily check shouldn't clobber a reason a person already attached) but refreshes
    actual_summary/ts so the row reflects the latest observation. A SYSTEM-authored
    reason (from clear_deviation_if_resolved's auto-resolution) is different: it's not
    testimony about this specific new observation, just a note that an earlier
    same-day deviation resolved itself -- if the scenario deviates again the same day,
    that auto-resolution note must be cleared back to unexplained, or a genuine new
    failure would silently hide behind a stale "auto-resolved" reason and vanish from
    get_deviations(unexplained_only=True) (found by Opus review, 2026-07-27, the same
    session clear_deviation_if_resolved switched from delete to auto-explain). See
    add_scenario_expectation for why this is an explicit COALESCE-based check-then-
    upsert rather than a UNIQUE-backed ON CONFLICT."""
    with _conn() as c:
        existing = c.execute("""
            SELECT id, reason_by FROM coverage_deviations
            WHERE check_date = ? AND scenario_key = ?
              AND COALESCE(node_id, -1) = COALESCE(?, -1)
              AND COALESCE(ticker, '') = COALESCE(?, '')
              AND COALESCE(mode, '') = COALESCE(?, '')
        """, (check_date, scenario_key, node_id, ticker, mode)).fetchone()
        if existing:
            if existing['reason_by'] == 'system':
                c.execute("""
                    UPDATE coverage_deviations
                    SET actual_summary=?, ts=datetime('now'), reason=NULL, reason_by=NULL, reason_ts=NULL
                    WHERE id=?
                """, (actual_summary, existing['id']))
            else:
                c.execute("""
                    UPDATE coverage_deviations SET actual_summary=?, ts=datetime('now')
                    WHERE id=?
                """, (actual_summary, existing['id']))
        else:
            c.execute("""
                INSERT INTO coverage_deviations
                    (check_date, scenario_key, ticker, node_id, mode, expected_outcome, actual_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (check_date, scenario_key, ticker, node_id, mode, expected_outcome, actual_summary))
        c.commit()


def clear_deviation_if_resolved(check_date, scenario_key, ticker=None, node_id=None, mode=None):
    """Auto-explains (never deletes) a same-day UNEXPLAINED deviation row for
    this (scenario_key, node_id, ticker, mode) if one exists -- called when a
    re-check finds the expectation now met. Every deviation row is a permanent
    record, like a ticket, once it exists -- an unexplained row that resolves
    on its own still gets a system-authored reason instead of vanishing, so
    there's no gap in the historical record of what happened (2026-07-27,
    replacing the prior delete-based behavior per user's explicit call: a
    deviation is an artifact of something that happened, not a transient flag
    to be erased once convenient). Only touches rows where reason IS NULL --
    a row a human already explained is untouched (see record_deviation's
    identical "never clobber a reason" rule). No-op if no matching
    unexplained row exists (the common case on a first, clean run)."""
    with _conn() as c:
        c.execute("""
            UPDATE coverage_deviations
            SET reason = 'Auto-resolved: scenario was met on a later same-day check.',
                reason_by = 'system', reason_ts = datetime('now')
            WHERE check_date = ? AND scenario_key = ? AND reason IS NULL
              AND COALESCE(node_id, -1) = COALESCE(?, -1)
              AND COALESCE(ticker, '') = COALESCE(?, '')
              AND COALESCE(mode, '') = COALESCE(?, '')
        """, (check_date, scenario_key, node_id, ticker, mode))
        c.commit()


def explain_deviation(deviation_id, reason, reason_by='user'):
    with _conn() as c:
        c.execute("""
            UPDATE coverage_deviations SET reason = ?, reason_by = ?, reason_ts = datetime('now')
            WHERE id = ?
        """, (reason, reason_by, deviation_id))
        c.commit()


def get_deviations(unexplained_only=False, check_date=None, limit=500):
    q = "SELECT * FROM coverage_deviations"
    clauses, params = [], []
    if unexplained_only:
        clauses.append("reason IS NULL")
    if check_date:
        clauses.append("check_date = ?")
        params.append(check_date)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def log_incident(title, detail, ticker=None, account=None, node_id=None, real_money_impact=False):
    with _conn() as c:
        c.execute("""
            INSERT INTO trading_incidents (title, detail, ticker, account, node_id, real_money_impact)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (title, detail, ticker, account, node_id, 1 if real_money_impact else 0))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def resolve_incident(incident_id, resolution, resolved_by='user'):
    with _conn() as c:
        c.execute("""
            UPDATE trading_incidents SET resolution = ?, resolved_by = ?, resolved_ts = datetime('now')
            WHERE id = ?
        """, (resolution, resolved_by, incident_id))
        c.commit()


def get_incidents(open_only=False, limit=200):
    q = "SELECT * FROM trading_incidents"
    if open_only:
        q += " WHERE resolved_ts IS NULL"
    q += " ORDER BY id DESC LIMIT ?"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (limit,)).fetchall()]


def snooze_coverage(scenario_key, snoozed_until, reason, ticker=None, account=None,
                     node_id=None, kind=None, snoozed_by='user'):
    """Records an acknowledgment that scenario_key should stop generating both
    its coverage_events row and its Slack alert until snoozed_until (a
    'YYYY-MM-DD HH:MM:SS' local-time string, matching SQLite's own
    datetime('now','localtime') format for a correct '>' comparison) for the
    given scope. Any of ticker/account/node_id/kind left None is a wildcard
    for that field, not "must be NULL" -- e.g.
    snooze_coverage('reconciliation_mismatch', until, reason, ticker='UDOW')
    silences UDOW across every account/node/kind, while adding
    kind='shares' too would narrow it to just the share-count-mismatch alert,
    leaving missing_sl/missing_trailing_sell (which mean a real position may
    be unprotected at the broker) still alerting for that same ticker. No
    dedup/upsert -- each call is its own record, consistent with
    coverage_deviations treating history as append-only."""
    with _conn() as c:
        c.execute("""
            INSERT INTO coverage_snoozes
                (scenario_key, ticker, account, node_id, kind, snoozed_until, reason, snoozed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (scenario_key, ticker, account, node_id, kind, snoozed_until, reason, snoozed_by))
        c.commit()


def is_snoozed(scenario_key, ticker=None, account=None, node_id=None, kind=None):
    """True if any active (not yet expired) coverage_snoozes row matches this
    firing. A snooze row's own ticker/account/node_id/kind are wildcards when
    NULL -- a row scoped only to ticker='UDOW' matches regardless of
    account/node_id/kind passed here, but a row additionally scoped to
    kind='shares' only matches a firing whose kind is exactly 'shares'."""
    with _conn() as c:
        row = c.execute("""
            SELECT 1 FROM coverage_snoozes
            WHERE scenario_key = ? AND snoozed_until > datetime('now', 'localtime')
              AND (ticker IS NULL OR ticker = ?)
              AND (account IS NULL OR account = ?)
              AND (node_id IS NULL OR node_id = ?)
              AND (kind IS NULL OR kind = ?)
            LIMIT 1
        """, (scenario_key, ticker, account, node_id, kind)).fetchone()
        return row is not None


def get_active_snoozes(scenario_key=None):
    q = "SELECT * FROM coverage_snoozes WHERE snoozed_until > datetime('now', 'localtime')"
    params = []
    if scenario_key:
        q += " AND scenario_key = ?"
        params.append(scenario_key)
    q += " ORDER BY id DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def get_closed_trades_for_ticker_on_date(ticker, check_date, strategy=None, version=None,
                                          window=None, account=None):
    """trade_log rows that both entered and exited on check_date (YYYY-MM-DD) --
    the 'same-day full lifecycle' shape coverage_check.py's trade_lifecycle
    check needs. Newest first. Ticker alone is ambiguous when a ticker has more
    than one real node (e.g. GDXU: watch_list ids 88 and 108, different
    accounts/versions on the same active watchlist) -- pass the node's
    disambiguators (from get_watch_list_node) to scope correctly, or a wrong
    node's trade can satisfy a different node's expectation (found by Opus
    review, 2026-07-24)."""
    q = "SELECT * FROM trade_log WHERE ticker = ? AND date(entry_time) = ? AND date(exit_time) = ?"
    params = [ticker, check_date, check_date]
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    if version:
        q += " AND version = ?"
        params.append(version)
    if window is not None:
        q += " AND window = ?"
        params.append(window)
    if account:
        q += " AND account = ?"
        params.append(account)
    q += " ORDER BY id DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def get_pending_buys_for_ticker_on_date(ticker, check_date, strategy=None, version=None,
                                         window=None, account=None):
    """See get_closed_trades_for_ticker_on_date for why ticker alone is
    ambiguous. pending_buys has no real strategy/version/window/account
    columns (they live inside node_json), so disambiguation is a Python-side
    filter after the ticker/date SQL match, not a SQL WHERE clause."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT * FROM pending_buys WHERE ticker = ? AND date(signal_time) = ?
            ORDER BY id DESC
        """, (ticker, check_date)).fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    if not any([strategy, version, window is not None, account]):
        return rows
    out = []
    for r in rows:
        node = r['node'] or {}
        if strategy and node.get('strategy') != strategy:
            continue
        if version and node.get('version') != version:
            continue
        if window is not None and node.get('window') != window:
            continue
        if account and node.get('account') != account:
            continue
        out.append(r)
    return out


def log_slack_message(mode, text, error=None):
    """Fire-and-forget, same pattern as log_coverage_event -- never raises past
    a logging failure into the real Slack-posting control flow. `error` is the
    caught exception/HTTP-status string from the real send attempt (None means
    no error was caught) -- call this after attempting the send, not before,
    so a row actually reflects the outcome rather than just the intent."""
    try:
        with _conn() as c:
            c.execute("INSERT INTO slack_message_log (mode, text, error) VALUES (?, ?, ?)", (mode, text, error))
            c.commit()
    except Exception:
        pass


def get_slack_messages(mode=None, since=None, limit=200):
    q = "SELECT * FROM slack_message_log"
    clauses, params = [], []
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


# ---------------------------------------------------------------------------
# Open positions CRUD
# ---------------------------------------------------------------------------

def _pos_tables(paper):
    """paper_positions/paper_trade_log are schema-identical mirrors of
    open_positions/trade_log -- this picks which pair the caller means instead
    of duplicating every CRUD function below."""
    return ('paper_positions', 'paper_trade_log') if paper else ('open_positions', 'trade_log')


def get_open_positions(paper=False):
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM {positions_table} ORDER BY entry_time"
        ).fetchall()]
    for r in rows:
        r['trail_state'] = json.loads(r['trail_state']) if r.get('trail_state') else {}
    return rows


def get_open_position(ticker, paper=False):
    """Single-ticker lookup -- used to report what's actually live when a
    duplicate-position attempt is rejected (see open_position())."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {positions_table} WHERE ticker=? ORDER BY entry_time DESC LIMIT 1",
            (ticker,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['trail_state'] = json.loads(d['trail_state']) if d.get('trail_state') else {}
    return d


def get_open_position_for_account(ticker, account, paper=False):
    """(ticker, account)-keyed sibling of get_open_position() -- used by
    schwab_safety.check_order's oversell guard, which already has `account` in
    scope but no wl_id (check_order isn't threaded a node -- see docs/
    backlog_cache.md's wl_id refactor entry). Ticker-only would resolve to
    whichever position for this ticker has the latest entry_time regardless of
    account -- with 2 live nodes on the same ticker in different accounts (the
    refactor's own motivating configuration), a real SELL for the older
    position's account could be bound-checked against a newer position in a
    *different* account and wrongly rejected as an oversell."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {positions_table} WHERE ticker=? AND account=? ORDER BY entry_time DESC LIMIT 1",
            (ticker, account)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['trail_state'] = json.loads(d['trail_state']) if d.get('trail_state') else {}
    return d


def get_open_position_by_wl_id(wl_id, paper=False):
    """wl_id-keyed sibling of get_open_position() -- use this at any call site
    that already has a specific node's id in scope, since ticker alone is
    ambiguous once 2+ concurrent nodes exist for the same ticker (see
    docs/backlog_cache.md's wl_id refactor entry). get_open_position() itself
    is left ticker-only (widely relied on by the test suite/legacy callers as
    a single-position-per-ticker convenience lookup)."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {positions_table} WHERE wl_id=? ORDER BY entry_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['trail_state'] = json.loads(d['trail_state']) if d.get('trail_state') else {}
    return d


def get_position_by_id(position_id, paper=False):
    """Fresh single-row lookup by primary key -- used where a caller holds a
    possibly-stale in-memory position dict (e.g. notify_trailing_activated
    re-reading trail_state after check_sell_condition already wrote the
    armed state to the DB) and must not merge onto stale fields."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(f"SELECT * FROM {positions_table} WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['trail_state'] = json.loads(d['trail_state']) if d.get('trail_state') else {}
    return d


def get_held_tickers():
    """Single source of truth for 'is this ticker already held' -- use this instead of
    re-deriving a ticker set from get_open_positions() at each call site. A prior version
    of this exact gap (one code path filtered on it, another didn't) caused a real
    spurious-BUY-alert bug on 2026-07-08; a second, separate instance of the same gap
    (send_reference_report never filtered at all) was found 2026-07-09."""
    return {p['ticker'] for p in get_open_positions()}


_PENDING_BUY_NODE_KEYS = ('id', 'ticker', 'strategy', 'version', 'window', 'take_profit', 'stop_loss',
                          'max_hold_hours', 'label', 'trail_sell_pct', 'fixed_sl', 'trail_buy_pct',
                          'arm_sell_pct', 'account', 'starting_notional')


def add_pending_buy(node, sig, channel, ts, order_id=None):
    node_subset = {k: node.get(k) for k in _PENDING_BUY_NODE_KEYS}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_buys (ticker, node_json, signal_price, signal_time, "
            "reminder_channel, reminder_ts, reminder_count, last_reminder_at, created_at, order_id, wl_id, "
            "running_low) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), channel, ts, now_str, now_str, order_id,
             node.get('id'), sig['current_price']),
        )
        c.commit()


def set_pending_buy_order_id(ticker, order_id):
    """Mirrors mark_pending_buy_placed's shape -- called once the broker order id
    is known (e.g. after check_gap_resize replaces a resting order with a market
    order, or if a future automated-buy path captures it post-hoc).
    Ticker-keyed -- kept for legacy/single-node-per-ticker callers; use
    set_pending_buy_order_id_by_wl_id where a specific node's id is in scope."""
    with _conn() as c:
        c.execute("UPDATE pending_buys SET order_id=? WHERE ticker=?", (order_id, ticker))
        c.commit()


def set_pending_buy_order_id_by_wl_id(wl_id, order_id):
    with _conn() as c:
        c.execute("UPDATE pending_buys SET order_id=? WHERE wl_id=?", (order_id, wl_id))
        c.commit()


def mark_gap_resize_attempted(pending_id, date_str):
    """Persisted idempotency guard -- see ensure_tables' gap_resize_date comment.
    Called once check_gap_resize has confirmed a row's gap condition and is about
    to cancel/replace its resting order, before doing so, so a restart mid-attempt
    can't re-enter and act on the same row twice today. Keyed on pending_buys.id
    (always present) rather than wl_id -- a NULL wl_id would make a wl_id-keyed
    UPDATE match zero rows and silently fail to persist the guard (found via
    Opus review, 2026-07-27)."""
    with _conn() as c:
        c.execute("UPDATE pending_buys SET gap_resize_date=? WHERE id=?", (date_str, pending_id))
        c.commit()


def update_pending_buy_running_low(wl_id, running_low):
    """Mirrors update_paper_pending_buy_running_low -- only ever called for a
    dry_run-account trailing buy (signals_notify.update_dry_run_buys), since a
    real trailing-buy order's running low is tracked by the broker itself."""
    with _conn() as c:
        c.execute("UPDATE pending_buys SET running_low=? WHERE wl_id=?", (running_low, wl_id))
        c.commit()


def get_pending_buys():
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM pending_buys").fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    return rows


def clear_pending_buy(ticker):
    """Ticker-keyed -- kept for legacy/single-node-per-ticker callers; use
    clear_pending_buy_by_wl_id where a specific node's id is in scope (deletes
    only that node's row instead of every pending_buys row for the ticker)."""
    with _conn() as c:
        c.execute("DELETE FROM pending_buys WHERE ticker = ?", (ticker,))
        c.commit()


def clear_pending_buy_by_wl_id(wl_id):
    with _conn() as c:
        c.execute("DELETE FROM pending_buys WHERE wl_id = ?", (wl_id,))
        c.commit()


def mark_pending_buy_placed(ticker):
    """Order confirmed resting at the broker -- stops the 'is it placed' nag, but
    doesn't open a position (mirrors trail_state.order_placed on the sell side: a
    placed order still needs a real fill before anything is actually held).
    Resets reminder_count/last_reminder_at so the fill-confirmation phase gets
    its own reminder numbering (#1, #2, ...) instead of continuing the placement
    phase's count -- the two are different questions ('is it placed?' vs 'did it
    fill?') and sharing one counter across them reads as a lie about how many
    times you've actually been asked about the fill.
    Ticker-keyed -- kept for legacy/single-node-per-ticker callers; use
    mark_pending_buy_placed_by_wl_id where a specific node's id is in scope."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET order_placed=1, reminder_count=0, last_reminder_at=? WHERE ticker = ?",
            (now_str, ticker),
        )
        c.commit()


def mark_pending_buy_placed_by_wl_id(wl_id):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET order_placed=1, reminder_count=0, last_reminder_at=? WHERE wl_id = ?",
            (now_str, wl_id),
        )
        c.commit()


def update_pending_buy_reminder(pending_id, channel, ts, reminder_count):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET reminder_channel=?, reminder_ts=?, reminder_count=?, last_reminder_at=? "
            "WHERE id=?",
            (channel, ts, reminder_count, now_str, pending_id),
        )
        c.commit()


_PAPER_PENDING_BUY_NODE_KEYS = _PENDING_BUY_NODE_KEYS  # starting_notional now included in the base tuple


def add_paper_pending_buy(node, sig):
    """No reminder machinery, unlike add_pending_buy -- a paper fill is
    auto-detected every poll (paper_trading.update_paper_buys), never confirmed
    by a human click. Keeps starting_notional (unlike the real pending_buys node
    subset) since update_paper_buys sizes the simulated fill directly off it,
    with no live watch_list node to fall back on the way the real Filled-button
    flow does."""
    node_subset = {k: node.get(k) for k in _PAPER_PENDING_BUY_NODE_KEYS}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "INSERT INTO paper_pending_buys (ticker, node_json, signal_price, signal_time, "
            "running_low, created_at, wl_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), sig['current_price'], now_str, node.get('id')),
        )
        c.commit()


def get_paper_pending_buys():
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM paper_pending_buys").fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    return rows


def get_paper_pending_buy(wl_id):
    """wl_id-keyed -- only caller is paper_trading.py, which always has the
    node's id in scope; ticker alone would match another concurrent node's
    pending row (see docs/backlog_cache.md's wl_id refactor entry)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM paper_pending_buys WHERE wl_id = ?", (wl_id,)).fetchone()
    return dict(row) if row else None


def clear_paper_pending_buy(wl_id):
    with _conn() as c:
        c.execute("DELETE FROM paper_pending_buys WHERE wl_id = ?", (wl_id,))
        c.commit()


def update_paper_pending_buy_running_low(pending_id, running_low):
    with _conn() as c:
        c.execute("UPDATE paper_pending_buys SET running_low = ? WHERE id = ?",
                  (float(running_low), pending_id))
        c.commit()


def update_position_trail_state(position_id, state, paper=False):
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        c.execute(f"UPDATE {positions_table} SET trail_state = ? WHERE id = ?",
                  (json.dumps(state), position_id))


def closed_today(ticker, paper=False, node=None):
    """True if this ticker had a trade_log exit today -- IRA/SEP cash accounts can't
    reuse that capital until T+1 settlement, so a same-day re-buy needs a warning.
    paper=True checks paper_trade_log instead -- found by Opus review 2026-07-24:
    the real-only default meant active_signals's buy_alerted same-day unlock
    (which calls this to detect a genuine close) could never fire for a
    paper-trading node, since paper fills only ever land in paper_trade_log.
    Ticker-only is the *correct* granularity for the cash-settlement warning
    callers (signals_notify/signals_blocks, schwab_safety.check_order) -- a
    real account's T+1 settlement constraint applies to the account/ticker
    regardless of which node closed the trade. The optional `node` narrows to
    (ticker, strategy, version, window, account) for the one caller asking a
    different question -- "did *this node's own* position close today" (the
    buy_alerted same-day re-arm unlock in active_signals.py) -- where ticker-
    only would let one node's exit wrongly re-arm a sibling node's lock when
    2+ nodes share a ticker (see docs/backlog_cache.md's wl_id refactor entry).
    Excludes is_dry_run_sim rows -- a synthesized dry-run exit is not a real
    settlement event and must never suppress/warn on a real same-day re-buy
    in a different (possibly live) account for the same ticker (Opus review
    2026-07-26)."""
    _, trade_log_table = _pos_tables(paper)
    today = datetime.now().strftime('%Y-%m-%d')
    with _conn() as c:
        if node is not None:
            row = c.execute(
                f"SELECT 1 FROM {trade_log_table} WHERE ticker=? AND strategy=? AND version=? AND window=? "
                f"AND COALESCE(account,'')=COALESCE(?,'') AND exit_time LIKE ? AND is_dry_run_sim=0 LIMIT 1",
                (ticker, node.get('strategy'), node.get('version'), node.get('window'),
                 node.get('account'), f"{today}%"),
            ).fetchone()
        else:
            row = c.execute(
                f"SELECT 1 FROM {trade_log_table} WHERE ticker = ? AND exit_time LIKE ? AND is_dry_run_sim=0 LIMIT 1",
                (ticker, f"{today}%"),
            ).fetchone()
    return row is not None


def open_position(node, signal_price, signal_time, entry_price, entry_time, shares=None, paper=False,
                   is_dry_run_sim=False):
    """Returns True if a position was opened, False if skipped because one was
    already open for this node — callers that report success to Slack
    must check this, since a silent skip must not be reported as a fill.
    Dedup keys on node['id'] (wl_id), not (ticker, window) -- two concurrent
    nodes could otherwise share a window and collide (see docs/backlog_cache.md's
    wl_id refactor entry).
    is_dry_run_sim tags a fill synthesized against real price data because the
    account is dry_run (no real broker fill will ever arrive) -- mutually
    exclusive with paper (a dry_run node is always mode='live', never research)."""
    positions_table, _ = _pos_tables(paper)
    with _position_lock, _conn() as c:
        # OR (wl_id IS NULL AND ticker=? AND window=?): a legacy position
        # predating the wl_id migration (or one the backfill couldn't
        # uniquely resolve, e.g. duplicated across watchlists) has wl_id=NULL
        # -- `wl_id=?` alone would never match it, silently reopening a
        # duplicate for a still-live node whose ticker+window it shares. This
        # preserves the original (ticker, window) protection for exactly
        # those unbackfilled rows, without weakening the wl_id check for
        # everything else.
        existing = c.execute(
            f"SELECT id FROM {positions_table} WHERE wl_id=? OR (wl_id IS NULL AND ticker=? AND window=?)",
            (node['id'], node['ticker'], int(node['window']))
        ).fetchone()
        if existing:
            print(f"  [warn] {'paper ' if paper else ''}position already open for {node['ticker']} wl_id={node['id']} — skipping duplicate")
            return False
        sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
        entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
        trade_log_id = log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares,
                                        paper=paper, is_dry_run_sim=is_dry_run_sim)
        tp = node.get('take_profit')
        c.execute(f"""
            INSERT INTO {positions_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, trade_log_id,
                 trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, shares, account, wl_id, is_dry_run_sim)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
            node.get('trail_sell_pct'), node.get('fixed_sl'), node.get('trail_buy_pct'),
            node.get('arm_sell_pct'), float(shares) if shares is not None else None,
            node.get('account'), node.get('id'), 1 if is_dry_run_sim else 0,
        ))
        c.commit()
        return True


def top_up_position(wl_id, additional_shares, fill_price, paper=False):
    """Adds top-up shares to an already-open position, blending entry_price by
    share-weighted average -- used by signals_notify._reconcile_fill (Part 3,
    branch C) when a real fill under-spent target_notional relative to the
    conservative worst-case sizing pads (branch A/B). Returns False if no open
    position exists for this node (nothing to top up). Keyed on wl_id, not
    ticker -- ticker-only would blend the wrong node's shares/entry_price if
    2+ nodes hold the same ticker concurrently."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT shares, entry_price FROM {positions_table} WHERE wl_id=?", (wl_id,)
        ).fetchone()
        if not row or not row['shares']:
            return False
        old_shares, old_entry = row['shares'], row['entry_price']
        new_shares = old_shares + additional_shares
        blended_entry = (old_shares * old_entry + additional_shares * fill_price) / new_shares
        c.execute(
            f"UPDATE {positions_table} SET shares=?, entry_price=? WHERE wl_id=?",
            (new_shares, blended_entry, wl_id),
        )
        c.commit()
    return True


def set_broker_stop_price(ticker, broker_stop_price):
    with _conn() as c:
        c.execute(
            "UPDATE open_positions SET broker_stop_price = ? WHERE ticker = ?",
            (float(broker_stop_price), ticker)
        )
        c.commit()


def set_sl_order_id(ticker, sl_order_id):
    """Records the resting STOP order's broker order id (Part 4, Section 6) --
    read back by _attempt_automated_sell to cancel it before placing the
    trailing-sell order on arm, avoiding two live sell orders for the same
    shares. Ticker-keyed -- kept for legacy/single-node-per-ticker callers
    (and the existing test suite); use set_sl_order_id_by_position where the
    position's own PK is in scope, since 2 concurrent same-ticker positions
    would otherwise both get the same sl_order_id written."""
    with _conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id = ? WHERE ticker = ?", (sl_order_id, ticker))
        c.commit()


def set_sl_order_id_by_position(position_id, sl_order_id):
    with _conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id = ? WHERE id = ?", (sl_order_id, position_id))
        c.commit()


def log_open_price_quality(ticker, target_h, target_m, price, is_true_open):
    with _conn() as c:
        c.execute(
            "INSERT INTO open_price_quality_log (ticker, target_h, target_m, price, is_true_open) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, target_h, target_m, float(price), 1 if is_true_open else 0),
        )
        c.commit()


def get_open_price_quality_log(since=None):
    with _conn() as c:
        if since:
            rows = c.execute(
                "SELECT * FROM open_price_quality_log WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM open_price_quality_log ORDER BY ts").fetchall()
        return [dict(r) for r in rows]


def correct_entry_price(ticker, entry_price):
    """Applies a corporate-action correction to a held position's entry_price
    -- fixing the underlying data is what clears a corporate-action freeze
    (signals_compute.check_sell_condition), not a separate unfreeze step."""
    with _conn() as c:
        c.execute(
            "UPDATE open_positions SET entry_price = ? WHERE ticker = ?",
            (float(entry_price), ticker)
        )
        c.commit()


def close_position(position_id, exit_signal_price=None, exit_price=None, exit_time=None, exit_reason=None, paper=False):
    """Returns True if this call actually closed the position, False if it was
    already gone -- callers that report a close to Slack must check this,
    same shape as open_position()'s duplicate-skip return. Guarded by the
    same _position_lock as open_position() -- without it, the poll loop and
    the Slack handler could both see the row still present, both write a
    trade_log exit (last write silently wins, possibly with the wrong
    price/reason), and only then race on the DELETE."""
    positions_table, _ = _pos_tables(paper)
    with _position_lock, _conn() as c:
        row = c.execute(
            f"SELECT trade_log_id, entry_price FROM {positions_table} WHERE id = ?", (position_id,)
        ).fetchone()
        if row is None:
            return False
        if exit_price is not None and row[0]:
            log_trade_exit(row[0], exit_signal_price, exit_price, exit_time, exit_reason, row[1], paper=paper)
        c.execute(f"DELETE FROM {positions_table} WHERE id = ?", (position_id,))
        c.commit()
        return True


def log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares=None, paper=False,
                     is_dry_run_sim=False):
    _, trade_log_table = _pos_tables(paper)
    sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    entry_drift    = (entry_price - signal_price) / signal_price * 100
    tp = node.get('take_profit')
    with _conn() as c:
        c.execute(f"""
            INSERT INTO {trade_log_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct, arm_sell_pct, shares, account,
                 is_dry_run_sim)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, entry_drift, node.get('arm_sell_pct'),
            float(shares) if shares is not None else None, node.get('account'), 1 if is_dry_run_sim else 0,
        ))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def log_trade_exit(trade_id, exit_signal_price, exit_price, exit_time, exit_reason, entry_price, paper=False):
    _, trade_log_table = _pos_tables(paper)
    exit_time_str = exit_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(exit_time, 'strftime') else exit_time
    exit_drift    = (exit_price - exit_signal_price) / exit_signal_price * 100
    pnl           = (exit_price - entry_price) / entry_price * 100
    with _conn() as c:
        c.execute(f"""
            UPDATE {trade_log_table} SET
                exit_signal_price = ?, exit_price = ?, exit_time = ?,
                exit_drift_pct = ?, pnl_pct = ?, exit_reason = ?
            WHERE id = ?
        """, (float(exit_signal_price), float(exit_price), exit_time_str,
              exit_drift, pnl, exit_reason, trade_id))
        c.commit()
