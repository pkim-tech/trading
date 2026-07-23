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
                result        TEXT,
                detail        TEXT
            )
        """)
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
                text     TEXT NOT NULL
            )
        """)
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
             fixed_sl_override=None):
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
        cur = c.execute("""
            INSERT OR IGNORE INTO watch_list
                (watchlist_id, mode, ticker, strategy, version, window, take_profit,
                 stop_loss, max_hold_hours, label, z_score_threshold, trail_sell_pct, fixed_sl,
                 trail_buy_pct, arm_sell_pct, entry_timing, starting_notional)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watchlist_id, mode, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours), label, float(z_score_threshold),
              stored_trail_sell_pct, fixed_sl, stored_trail_buy_pct, stored_arm_sell_pct,
              entry_timing, float(starting_notional)))
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


def log_coverage_event(scenario_key, mode, ticker=None, position_id=None, result='', detail=''):
    """Records one real firing of an automation control/phase, tagged by which
    environment exercised it. `mode` is one of 'paper'/'dry_run'/'live' -- the
    caller determines this from its own context (e.g. paper_trading.py always
    passes 'paper'; schwab_safety/signals_notify pass 'live' or 'dry_run' based
    on the account's real dry_run flag), not inferred here. Fire-and-forget:
    never raises past a logging failure into the caller's real control flow."""
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO coverage_events (scenario_key, mode, ticker, position_id, result, detail)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (scenario_key, mode, ticker, position_id, result, detail))
            c.commit()
    except Exception:
        pass


def get_coverage_events(scenario_key=None, mode=None, limit=500):
    q = "SELECT * FROM coverage_events"
    clauses, params = [], []
    if scenario_key:
        clauses.append("scenario_key = ?")
        params.append(scenario_key)
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def log_slack_message(mode, text):
    """Fire-and-forget, same pattern as log_coverage_event -- never raises past
    a logging failure into the real Slack-posting control flow."""
    try:
        with _conn() as c:
            c.execute("INSERT INTO slack_message_log (mode, text) VALUES (?, ?)", (mode, text))
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
    return dict(row) if row else None


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


_PENDING_BUY_NODE_KEYS = ('ticker', 'strategy', 'version', 'window', 'take_profit', 'stop_loss',
                          'max_hold_hours', 'label', 'trail_sell_pct', 'fixed_sl', 'trail_buy_pct',
                          'arm_sell_pct', 'account', 'starting_notional')


def add_pending_buy(node, sig, channel, ts, order_id=None):
    node_subset = {k: node.get(k) for k in _PENDING_BUY_NODE_KEYS}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_buys (ticker, node_json, signal_price, signal_time, "
            "reminder_channel, reminder_ts, reminder_count, last_reminder_at, created_at, order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), channel, ts, now_str, now_str, order_id),
        )
        c.commit()


def set_pending_buy_order_id(ticker, order_id):
    """Mirrors mark_pending_buy_placed's shape -- called once the broker order id
    is known (e.g. after check_gap_resize replaces a resting order with a market
    order, or if a future automated-buy path captures it post-hoc)."""
    with _conn() as c:
        c.execute("UPDATE pending_buys SET order_id=? WHERE ticker=?", (order_id, ticker))
        c.commit()


def get_pending_buys():
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM pending_buys").fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    return rows


def clear_pending_buy(ticker):
    with _conn() as c:
        c.execute("DELETE FROM pending_buys WHERE ticker = ?", (ticker,))
        c.commit()


def mark_pending_buy_placed(ticker):
    """Order confirmed resting at the broker -- stops the 'is it placed' nag, but
    doesn't open a position (mirrors trail_state.order_placed on the sell side: a
    placed order still needs a real fill before anything is actually held).
    Resets reminder_count/last_reminder_at so the fill-confirmation phase gets
    its own reminder numbering (#1, #2, ...) instead of continuing the placement
    phase's count -- the two are different questions ('is it placed?' vs 'did it
    fill?') and sharing one counter across them reads as a lie about how many
    times you've actually been asked about the fill."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET order_placed=1, reminder_count=0, last_reminder_at=? WHERE ticker = ?",
            (now_str, ticker),
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
            "running_low, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), sig['current_price'], now_str),
        )
        c.commit()


def get_paper_pending_buys():
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM paper_pending_buys").fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    return rows


def get_paper_pending_buy(ticker):
    with _conn() as c:
        row = c.execute("SELECT * FROM paper_pending_buys WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row else None


def clear_paper_pending_buy(ticker):
    with _conn() as c:
        c.execute("DELETE FROM paper_pending_buys WHERE ticker = ?", (ticker,))
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


def closed_today(ticker):
    """True if this ticker had a trade_log exit today -- IRA/SEP cash accounts can't
    reuse that capital until T+1 settlement, so a same-day re-buy needs a warning."""
    today = datetime.now().strftime('%Y-%m-%d')
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM trade_log WHERE ticker = ? AND exit_time LIKE ? LIMIT 1",
            (ticker, f"{today}%"),
        ).fetchone()
    return row is not None


def open_position(node, signal_price, signal_time, entry_price, entry_time, shares=None, paper=False):
    """Returns True if a position was opened, False if skipped because one was
    already open for this ticker/window — callers that report success to Slack
    must check this, since a silent skip must not be reported as a fill."""
    positions_table, _ = _pos_tables(paper)
    with _position_lock, _conn() as c:
        existing = c.execute(
            f"SELECT id FROM {positions_table} WHERE ticker=? AND window=?",
            (node['ticker'], int(node['window']))
        ).fetchone()
        if existing:
            print(f"  [warn] {'paper ' if paper else ''}position already open for {node['ticker']} w={node['window']} — skipping duplicate")
            return False
        sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
        entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
        trade_log_id = log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares, paper=paper)
        tp = node.get('take_profit')
        c.execute(f"""
            INSERT INTO {positions_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, trade_log_id,
                 trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, shares, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
            node.get('trail_sell_pct'), node.get('fixed_sl'), node.get('trail_buy_pct'),
            node.get('arm_sell_pct'), float(shares) if shares is not None else None,
            node.get('account'),
        ))
        c.commit()
        return True


def top_up_position(ticker, additional_shares, fill_price, paper=False):
    """Adds top-up shares to an already-open position, blending entry_price by
    share-weighted average -- used by signals_notify._reconcile_fill (Part 3,
    branch C) when a real fill under-spent target_notional relative to the
    conservative worst-case sizing pads (branch A/B). Returns False if no open
    position exists for the ticker (nothing to top up)."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT shares, entry_price FROM {positions_table} WHERE ticker=?", (ticker,)
        ).fetchone()
        if not row or not row['shares']:
            return False
        old_shares, old_entry = row['shares'], row['entry_price']
        new_shares = old_shares + additional_shares
        blended_entry = (old_shares * old_entry + additional_shares * fill_price) / new_shares
        c.execute(
            f"UPDATE {positions_table} SET shares=?, entry_price=? WHERE ticker=?",
            (new_shares, blended_entry, ticker),
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
    shares."""
    with _conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id = ? WHERE ticker = ?", (sl_order_id, ticker))
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


def log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares=None, paper=False):
    _, trade_log_table = _pos_tables(paper)
    sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    entry_drift    = (entry_price - signal_price) / signal_price * 100
    tp = node.get('take_profit')
    with _conn() as c:
        c.execute(f"""
            INSERT INTO {trade_log_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct, arm_sell_pct, shares, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, entry_drift, node.get('arm_sell_pct'),
            float(shares) if shares is not None else None, node.get('account'),
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
