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

# Tickers refused in any IRA/Roth/SEP-type account by add_node -- typically
# commodity-futures ETFs structured as K-1-issuing limited partnerships, which
# can generate real UBTI/UBIT in a tax-advantaged account. See CLAUDE.md's
# "Ticker exclusion, decided 2026-08-04" note. Confirm real K-1 status before
# removing an entry here.
# USO: commodity-futures ETF, presumed K-1 (never directly confirmed, backlog
#   research candidate killed on this basis before ever being funded).
# AGQ: confirmed K-1 (ProShares' own tax documentation, 2026-08-04) -- Section
#   1256 commodity pool. Real v5 watchlist ticker; decided 2026-08-04 to run in
#   taxable brokerage instead (no wash-sale rule + favorable 60/40 blended rate
#   there), NOT in IRA/Roth/SEP (real UBTI/Form 990-T exposure). See
#   docs/backlog_cache.md's 2026-08-04 AGQ entries for the full reasoning.
TAX_ADVANTAGED_EXCLUDED_TICKERS = {"USO", "AGQ"}


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
            if 'mode' not in wl_cols and 'state' not in wl_cols:
                # Only re-add 'mode' for a genuinely pre-migration DB (one that has
                # neither column yet) -- the later state migration block reads it to
                # backfill 'state' before dropping it for good. Once 'state' exists,
                # 'mode' must stay gone: this branch used to fire unconditionally on
                # every ensure_tables() call, silently resurrecting 'mode' (default
                # 'live', on EVERY row) immediately after the state migration dropped
                # it -- confirmed via Opus review reproducing this against a copy of
                # the real production DB, 2026-08-06.
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
        if 'paper_role' not in wl_cols:
            # NULL (default) = ordinary node -- either a real/live node or the existing
            # free-running paper "live-track" (live-tick pricing, no reconciliation).
            # 'daily_sync' tags the 2026-08-05 "daily-track" sibling: same ticker/
            # strategy/config, but signal-checked against the last closed hourly bar's
            # Close (see compute_buy_signal) instead of a live tick, and nightly
            # CLASSIFIED against a fresh backtest replay -- paper_trading.
            # reconcile_daily_track_nodes is pure observation (final design, corrected
            # mid-session after an earlier reconcile-and-resync/halt version): it logs
            # whether each night's state matches, and whether a mismatch is explainable
            # by the Open-vs-Close price-source difference, but never opens/closes a
            # position or halts the node itself. Isolates price-source timing as the
            # variable under test against the backtest. See docs/design.md's
            # "Two-account paper trading" section.
            c.execute("ALTER TABLE watch_list ADD COLUMN paper_role TEXT")
        if 'daily_sync_halted_at' not in wl_cols:
            # NOT set by reconcile_daily_track_nodes (pure observation, no state
            # mutation -- see paper_role above) -- this is inert scaffolding for a
            # separate, not-yet-built "sync" tool (the user's framing, 2026-08-05:
            # "reconcile as one action (how far are we) vs sync (prepare for next
            # day)"). start_paper_buy/start_paper_market_buy still check it and skip
            # entry when set, so a future sync tool has a real lever to pull, but
            # nothing in the current codebase ever sets it.
            c.execute("ALTER TABLE watch_list ADD COLUMN daily_sync_halted_at TEXT")
        if 'daily_track_bookmark_signal_bar' not in wl_cols:
            # Added 2026-08-10 -- fixes a real false-positive bug in
            # reconcile_daily_track_nodes: it always compared daily-track's
            # actual state against the backtest's single LATEST trade
            # (trades[-1]), which is wrong whenever daily-track is still
            # legitimately mid-trade on an EARLIER backtest trade (its own
            # hold duration differs from the backtest's for that same
            # entry -- exactly the timing divergence this tool exists to
            # detect) -- every night in between misclassified as
            # 'ambiguous_position'/unexplained, not a real bug.
            # This bookmark tracks "the signal-bar timestamp of the last
            # backtest trade this node has been fully, conclusively
            # reconciled through" -- reconcile always targets the earliest
            # backtest trade with signal_bar > bookmark (not trades[-1]),
            # and only advances the bookmark once that specific trade's
            # comparison reaches a terminal verdict (see
            # _daily_track_comparison_terminal in paper_trading.py -- revised
            # 2026-08-10 after a paired review found the first version's
            # terminal boundary wrong on two of its four cases).
            # Deliberately NOT a position-state reset (the user's explicit
            # 2026-08-05 call against auto-resync stands unchanged) -- this
            # only tracks which comparison is in progress, never touches a
            # real open_positions/paper_positions row. A single reconcile
            # pass can catch up through and advance past several already-
            # resolved trades in one call, not just one per calendar day --
            # see reconcile_daily_track_nodes' docstring.
            c.execute("ALTER TABLE watch_list ADD COLUMN daily_track_bookmark_signal_bar TEXT")
        if 'drought_overlay_enabled' not in wl_cols:
            # 2026-08-09 -- generic config toggle for the drought overlay (buy the
            # underlying during a confirmed low-vol no-signal gap, manage with the
            # node's own SL/arm/trail state machine), per docs/design.md's 2026-08-07
            # "Live automation design" section. Not ticker-specific code -- any node
            # can turn this on with its own confirm_days/vol_gate below, same as any
            # other strategy parameter on this table.
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_overlay_enabled INTEGER NOT NULL DEFAULT 0")
        if 'drought_confirm_days' not in wl_cols:
            # Mirrors scripts/stacked_model/drought.py::generate_drought_trades'
            # confirm_days param name directly, so live and backtest code share one
            # vocabulary. NULL when drought_overlay_enabled=0.
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_vol_gate' not in wl_cols:
            # Mirrors generate_drought_trades' vol_gate param (percentile 0-1, None =
            # no gate) -- NULL here means no gate, matching the backtest default.
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_vol_gate REAL")
        if 'drought_sl_pct_override' not in wl_cols:
            # Mirrors generate_drought_trades' sl_pct/arm_pct/trail_pct params --
            # NULL (default, and the only validated setting so far: SOXL's real
            # signal, docs/research_log.md's 2026-08-05/06 entries, explicitly
            # reuses the node's OWN core fixed_sl/arm_sell_pct/trail_sell_pct; every
            # independently-tuned non-default variant tested failed cliff-safety or
            # out-of-sample) means "use this node's own core risk params", same as
            # the backtest function's own default. Added now, ahead of any current
            # need, specifically so a future re-tuning attempt (this axis has
            # already been revisited once and may be again) doesn't need another
            # migration -- non-NULL here would be an explicit, deliberate deviation
            # from today's validated config, not a silent behavior change.
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_sl_pct_override REAL")
        if 'drought_arm_pct_override' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_arm_pct_override REAL")
        if 'drought_trail_pct_override' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN drought_trail_pct_override REAL")
        if 'addon_enabled' not in wl_cols:
            # Margin add-on-at-arm: no separate parameters -- always borrows 100% of
            # the position's current market value the moment trail_state['trailing']
            # flips True (the same event notify_trailing_activated already detects),
            # per the 2026-08-07/08 validated sizing rule. A different sizing rule
            # would need new backtest work first, not just a new column here.
            c.execute("ALTER TABLE watch_list ADD COLUMN addon_enabled INTEGER NOT NULL DEFAULT 0")
        if 'skim_enabled' not in wl_cols:
            # Skim-and-reserve overlay (docs/deep_backlog.md's 2026-08-04
            # "decided-in-principle" entry): skims skim_frac of current strategy value
            # into a pooled SPY reserve whenever equity makes a new high >= skim_step
            # above the last skim reference. Redeploy is deliberately manual (a Slack
            # alert, not automated) -- see skim_ref/skim_peak_before_decline/
            # skim_min_since_peak below for the persisted state that alert needs.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_enabled INTEGER NOT NULL DEFAULT 0")
        if 'skim_step' not in wl_cols:
            # Nullable, no default -- NULL means "not deliberately configured yet",
            # matching drought_confirm_days/drought_vol_gate's own None-friendly
            # convention (Opus review, 2026-08-09: a NOT NULL DEFAULT would silently
            # backfill every existing node as if it had been deliberately tuned).
            # scripts/stacked_model/skim_reserve.SKIM_STEP is the validated fallback
            # a caller applies when this is NULL, not a DB-level default.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_step REAL")
        if 'skim_frac' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_frac REAL")
        if 'skim_reserve_balance' not in wl_cols:
            # Per-node LEDGER balance against the shared pooled SPY holding in
            # skim_reserve_pool (per docs/backlog_cache.md's 2026-08-04 "later" note:
            # multiple nodes' skims land in one physical SPY position per account, so
            # shares aren't segregated -- this column is "how much of the pool is
            # this node's", not a share count). Redeploy sells min(this, needed) of
            # the pooled position and credits/debits this column.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_reserve_balance REAL NOT NULL DEFAULT 0")
        if 'skim_ref' not in wl_cols:
            # Last equity level a skim fired at (ratchets up on each skim, per
            # sim_skim_redeploy.skim_redeploy_overlay's validated logic) -- must
            # persist across daemon restarts, unlike the backtest's in-memory loop var.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_ref REAL")
        if 'skim_peak_before_decline' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_peak_before_decline REAL")
        if 'skim_min_since_peak' not in wl_cols:
            # Persisted tracker for the redeploy trigger -- see
            # scripts/stacked_model/skim_reserve.py's 2026-08-08 CRITICAL fix comment:
            # without this, a 0.1% wiggle right at the peak looks like ">= 80% of
            # peak" and fires the redeploy alert on noise, never having actually
            # declined. Same field, same reason, persisted instead of a loop-local var.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_min_since_peak REAL")
        if 'skim_declining' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_declining INTEGER NOT NULL DEFAULT 0")
        if 'skim_alert_sent_at' not in wl_cols:
            # Human-readable "when did we last alert" display field -- NOT the
            # idempotency gate itself (see skim_alert_80_sent/skim_alert_100_sent
            # below for that). The validated redeploy design has TWO independent
            # thresholds (80%/100% of the pre-decline peak,
            # scripts/stacked_model/skim_reserve.py's REDEPLOY_THRESHOLDS) that
            # can each fire once per decline cycle -- a single timestamp can't
            # distinguish which one(s) already fired, so this alone would have
            # under- or over-suppressed one of the two alerts.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_alert_sent_at TEXT")
        if 'skim_alert_80_sent' not in wl_cols:
            # Per-threshold idempotency guards, mirroring skim_reserve.py's
            # `armed` set (one entry per threshold, discarded once that
            # threshold fires, reset to "armed" only on a fresh post-recovery
            # high -- see manual_redeploy_overlay's `armed.discard`/
            # `armed = set(thresholds)` reset point). Both reset to 0 together
            # with skim_peak_before_decline whenever a fresh high is made
            # (the same "armed = set(thresholds)" moment), never independently.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_alert_80_sent INTEGER NOT NULL DEFAULT 0")
        if 'skim_alert_100_sent' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_alert_100_sent INTEGER NOT NULL DEFAULT 0")
        if 'skim_strategy_value' not in wl_cols:
            # Real dollar value currently deployed in the strategy sleeve --
            # added 2026-08-09 (Opus review fix) after the first version's skim
            # amount (`starting_notional * equity * skim_frac`, recomputed off
            # the FULL undiluted equity every time) was found to diverge from
            # the validated model in the wrong direction: manual_redeploy_overlay
            # skims a fraction of the CURRENTLY-DEPLOYED sleeve (w_strategy),
            # which shrinks by (1-skim_frac) at every skim, not the full
            # notional recomputed fresh each time. NULL = not yet initialized
            # (treated as starting_notional on first use, mirroring w_strategy's
            # own 1.0 starting weight applied to real dollars instead of a
            # normalized unit).
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_strategy_value REAL")
        if 'skim_last_mark_time' not in wl_cols:
            # Last time the reserve was marked to a real SPY price -- lets
            # check_paper_skim compute the reserve's own real return since the
            # prior mark (a genuine gap in the first version, flagged by
            # review: the reserve never actually earned/lost anything, so
            # paper couldn't measure the overlay's real net effect). NULL
            # until the first mark.
            c.execute("ALTER TABLE watch_list ADD COLUMN skim_last_mark_time TEXT")
        if 'state' not in wl_cols:
            # Collapses mode ('research'/'live') + the per-node dry_run
            # override (both discussed 2026-08-1x, dry_run itself only
            # landed hours earlier the same session before this collapse was
            # decided) into ONE column -- 'paper' / 'dry_run' / 'live',
            # mutually exclusive and exhaustive. Per the user's explicit
            # call: these are stored separately from the ACCOUNT-level ceiling
            # (schwab_safety.ACCOUNTS[account].trading_enabled, kept as a
            # real, independently-checked gate -- real_order_allowed =
            # (node.state == 'live') AND (account.trading_enabled == True)),
            # but a node's own mode+dry_run were two fields answering one
            # question ("what is this node doing right now") and reasoning
            # about two fields for one fact was the friction being removed.
            # 'paper' means paper_trading.py's simulation (mode=='research'
            # today) -- never consults schwab_client at all, a categorically
            # different thing from 'dry_run', which DOES run every real
            # check_order guard, just short-circuits at the final broker
            # call. Backfilled from the real mode/dry_run/account columns
            # below (needs schwab_safety.ACCOUNTS for the account-level
            # ceiling -- local import to break the signals_db<->schwab_safety
            # circular import, same pattern already used at check_order's
            # cash-check call site in schwab_safety.py).
            c.execute("ALTER TABLE watch_list ADD COLUMN state TEXT NOT NULL DEFAULT 'paper'")
            import schwab_safety
            # dry_run itself is brand new the same session as this collapse --
            # most real DBs never had it at all (only this session's own test
            # DBs, mid-refactor, might). Handle both.
            _had_dry_run_col = 'dry_run' in wl_cols
            select_cols = "id, mode, dry_run, account" if _had_dry_run_col else "id, mode, account"
            rows = c.execute(f"SELECT {select_cols} FROM watch_list").fetchall()
            for r in rows:
                if r['mode'] != 'live':
                    state = 'paper'
                else:
                    limits = schwab_safety.ACCOUNTS.get(r['account'])
                    account_dry = not bool(limits and limits.trading_enabled)
                    node_dry = bool(r['dry_run']) if _had_dry_run_col else False
                    state = 'dry_run' if (account_dry or node_dry) else 'live'
                c.execute("UPDATE watch_list SET state=? WHERE id=?", (state, r['id']))
            c.commit()
            c.execute("ALTER TABLE watch_list DROP COLUMN mode")
            if _had_dry_run_col:
                c.execute("ALTER TABLE watch_list DROP COLUMN dry_run")

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
                    state              TEXT NOT NULL DEFAULT 'paper',
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
                    paper_role         TEXT,
                    daily_sync_halted_at TEXT,
                    daily_track_bookmark_signal_bar TEXT,
                    drought_overlay_enabled INTEGER NOT NULL DEFAULT 0,
                    drought_confirm_days INTEGER,
                    drought_vol_gate   REAL,
                    drought_sl_pct_override REAL,
                    drought_arm_pct_override REAL,
                    drought_trail_pct_override REAL,
                    addon_enabled      INTEGER NOT NULL DEFAULT 0,
                    skim_enabled       INTEGER NOT NULL DEFAULT 0,
                    skim_step          REAL,
                    skim_frac          REAL,
                    skim_reserve_balance REAL NOT NULL DEFAULT 0,
                    skim_ref           REAL,
                    skim_peak_before_decline REAL,
                    skim_min_since_peak REAL,
                    skim_declining     INTEGER NOT NULL DEFAULT 0,
                    skim_alert_sent_at TEXT,
                    skim_alert_80_sent INTEGER NOT NULL DEFAULT 0,
                    skim_alert_100_sent INTEGER NOT NULL DEFAULT 0,
                    skim_strategy_value REAL,
                    skim_last_mark_time TEXT,
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

        # paper_role wasn't part of the UNIQUE constraint above (added later, 2026-08-05) --
        # without this, a daily-track node (paper_role='daily_sync', identical ticker/strategy/
        # config/account to its live-track sibling by design) would collide with it on insert. Same rebuild
        # pattern as the account fix above (SQLite can't ALTER a UNIQUE constraint in place).
        wl_schema_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watch_list'"
        ).fetchone()[0]
        if 'paper_role' not in wl_schema_sql.split('UNIQUE(')[-1]:
            c.execute("DROP TABLE IF EXISTS watch_list_new")
            c.executescript("""
                CREATE TABLE watch_list_new (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id       INTEGER NOT NULL,
                    state              TEXT NOT NULL DEFAULT 'paper',
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
                    paper_role         TEXT,
                    daily_sync_halted_at TEXT,
                    daily_track_bookmark_signal_bar TEXT,
                    drought_overlay_enabled INTEGER NOT NULL DEFAULT 0,
                    drought_confirm_days INTEGER,
                    drought_vol_gate   REAL,
                    drought_sl_pct_override REAL,
                    drought_arm_pct_override REAL,
                    drought_trail_pct_override REAL,
                    addon_enabled      INTEGER NOT NULL DEFAULT 0,
                    skim_enabled       INTEGER NOT NULL DEFAULT 0,
                    skim_step          REAL,
                    skim_frac          REAL,
                    skim_reserve_balance REAL NOT NULL DEFAULT 0,
                    skim_ref           REAL,
                    skim_peak_before_decline REAL,
                    skim_min_since_peak REAL,
                    skim_declining     INTEGER NOT NULL DEFAULT 0,
                    skim_alert_sent_at TEXT,
                    skim_alert_80_sent INTEGER NOT NULL DEFAULT 0,
                    skim_alert_100_sent INTEGER NOT NULL DEFAULT 0,
                    skim_strategy_value REAL,
                    skim_last_mark_time TEXT,
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit,
                           stop_loss, max_hold_hours, arm_sell_pct, trail_buy_pct,
                           trail_sell_pct, account, paper_role)
                );
            """)
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
        if 'signal_bar_time' not in op_cols:
            # signal_time itself is deliberately fill_time (wall-clock) for a trailing-buy
            # position (2026-07-31 hold-time-origin fix -- correct for real hold-budget
            # tracking) -- but that means it can't double as "which hourly bar detected the
            # signal" for daily-track's reconcile (paper_trading.reconcile_daily_track_nodes),
            # which needs the real bar. Nullable, populated only by
            # paper_trading.update_paper_buys' bounce-fill completion (the one path where
            # signal_time and the real signal bar diverge) from the pending buy's own
            # signal_time, which IS bar-aligned (paper_pending_buys rows are written with
            # sig['last_bar']). NULL for every other caller, where signal_time already is
            # bar-aligned (found by Opus review, 2026-08-05).
            c.execute("ALTER TABLE open_positions ADD COLUMN signal_bar_time TEXT")
        if 'position_source' not in op_cols:
            # 'core' (default) | 'drought_overlay' -- discriminator for the 2026-08-09
            # overlay-mechanism build (docs/design.md's 2026-08-07 "Live automation
            # design" section). Deliberately does NOT include 'addon_leg' -- a cold
            # Opus review (2026-08-09) found that an add-on leg sharing its parent's
            # ticker/wl_id would break every single-row assumption in this file
            # (get_open_position's ORDER BY entry_time DESC LIMIT 1 would silently
            # return the leg instead of core; top_up_position/set_broker_stop_price/
            # set_sl_order_id's ticker-keyed UPDATEs would clobber both rows;
            # get_held_tickers/closed_today would misattribute). Add-on legs live in
            # their own dedicated addon_legs/paper_addon_legs tables instead (below),
            # never touching open_positions/trade_log at all -- drought_overlay is
            # safe here specifically because it only ever opens while core is flat
            # for the same wl_id (no simultaneous-row case), so the existing
            # single-row lookups stay correct unmodified.
            c.execute("ALTER TABLE open_positions ADD COLUMN position_source TEXT NOT NULL DEFAULT 'core' "
                      "CHECK (position_source IN ('core', 'drought_overlay'))")
        if 'drought_confirm_days' not in op_cols:
            # Config snapshot at entry time, same reasoning as fixed_sl/trail_sell_pct
            # etc. above (staged_test_config's baseline-drift protection pattern) --
            # NULL except on position_source='drought_overlay' rows.
            c.execute("ALTER TABLE open_positions ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_vol_gate' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN drought_vol_gate REAL")
        if 'drought_gap_start' not in op_cols:
            # The OBSERVED no-signal gap start this entry actually fired on -- distinct
            # from drought_confirm_days/drought_vol_gate above (the threshold
            # CONFIG). reconcile_overlay_nodes' real question is "did the backtest
            # agree this gap was confirmed on this date," which needs the observed
            # value, not just the threshold that was in effect (Opus review finding).
            c.execute("ALTER TABLE open_positions ADD COLUMN drought_gap_start TEXT")
        if 'drought_vol_pctile' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN drought_vol_pctile REAL")

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
        if 'wl_id' not in tl_cols:
            # log_trade_entry's INSERT is shared between trade_log and paper_trade_log via
            # _pos_tables -- added here in lockstep with paper_trade_log's own wl_id add
            # below so that one INSERT statement can populate it unconditionally for both.
            c.execute("ALTER TABLE trade_log ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")
        if 'signal_bar_time' not in tl_cols:
            # See open_positions.signal_bar_time -- trade_log/paper_trade_log need the same
            # column (added lockstep, same shared-INSERT reason as wl_id above) so
            # reconcile_daily_track_nodes' pre-resync "did daily-track already close a trade
            # against this signal bar" check survives a position closing (open_positions rows
            # are deleted on close; only trade_log/paper_trade_log persist). Found missing by
            # a regression test, not a review round -- the open_positions/paper_positions add
            # alone silently degraded back to the wall-clock signal_time fallback for any
            # already-closed trade.
            c.execute("ALTER TABLE trade_log ADD COLUMN signal_bar_time TEXT")
        if 'exit_bar_time' not in tl_cols:
            # Same rationale as signal_bar_time, for the exit side -- exit_time is wall-clock
            # (datetime.now() at poll time), and for an at_bar_close exit specifically, that
            # wall-clock moment is always chronologically inside the NEXT bar's window (bar N
            # ends exactly when bar N+1 begins, and the poll detecting the close fires shortly
            # after), so reconcile_daily_track_nodes' _bar_containing(exit_time) lookup
            # misattributes every clean bar-close exit to the wrong bar -- confirmed by Opus
            # review, 2026-08-05. paper_trading.check_paper_sells captures the real graded bar
            # (last_bar_ts) explicitly for the at_bar_close branch; NULL for the mid-bar
            # reactive branch, where exit_time's wall clock genuinely does fall inside the bar
            # the trigger action occurred in and _bar_containing is the correct lookup.
            c.execute("ALTER TABLE trade_log ADD COLUMN exit_bar_time TEXT")
        if 'position_source' not in tl_cols:
            # See open_positions.position_source above -- kept in the closed-trade
            # record too since open_positions rows are deleted on close and
            # reconcile_overlay_nodes needs to tell drought trades apart from core
            # ones after the fact. No 'addon_leg' value here either -- see
            # open_positions.position_source's comment (addon lives in its own
            # addon_legs/paper_addon_legs tables).
            c.execute("ALTER TABLE trade_log ADD COLUMN position_source TEXT NOT NULL DEFAULT 'core' "
                      "CHECK (position_source IN ('core', 'drought_overlay'))")
        if 'drought_confirm_days' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_vol_gate' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN drought_vol_gate REAL")
        if 'drought_gap_start' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN drought_gap_start TEXT")
        if 'drought_vol_pctile' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN drought_vol_pctile REAL")

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
        if 'position_source' not in pb_cols:
            # Carries the drought discriminator through from signal to fill --
            # every real fill consumer (_reconcile_buy_fill, handle_trail_buy_fill_price,
            # handle_entry_price) must dispatch on this instead of defaulting to
            # open_position's 'core', or a real drought entry would silently land
            # as a core row (see docs/plans/real_order_execution_drought_addon.md 0.5).
            # No CHECK constraint here (SQLite ALTER TABLE ADD COLUMN can't add one) --
            # enforced in add_pending_buy instead, same pattern as open_positions'
            # table-creation-time constraint.
            c.execute("ALTER TABLE pending_buys ADD COLUMN position_source TEXT NOT NULL DEFAULT 'core'")
        if 'drought_confirm_days' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_vol_gate' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN drought_vol_gate REAL")
        if 'drought_gap_start' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN drought_gap_start TEXT")
        if 'drought_vol_pctile' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN drought_vol_pctile REAL")

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
        if 'signal_bar_time' not in pp_cols:
            # See open_positions.signal_bar_time above -- same rationale, kept
            # schema-identical for the same shared-INSERT reason as is_dry_run_sim.
            c.execute("ALTER TABLE paper_positions ADD COLUMN signal_bar_time TEXT")
        if 'position_source' not in pp_cols:
            # See open_positions.position_source above -- schema-identical for the
            # same shared-INSERT reason as is_dry_run_sim/signal_bar_time. No
            # 'addon_leg' value (see that comment) -- addon uses paper_addon_legs.
            c.execute("ALTER TABLE paper_positions ADD COLUMN position_source TEXT NOT NULL DEFAULT 'core' "
                      "CHECK (position_source IN ('core', 'drought_overlay'))")
        if 'drought_confirm_days' not in pp_cols:
            c.execute("ALTER TABLE paper_positions ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_vol_gate' not in pp_cols:
            c.execute("ALTER TABLE paper_positions ADD COLUMN drought_vol_gate REAL")
        if 'drought_gap_start' not in pp_cols:
            c.execute("ALTER TABLE paper_positions ADD COLUMN drought_gap_start TEXT")
        if 'drought_vol_pctile' not in pp_cols:
            c.execute("ALTER TABLE paper_positions ADD COLUMN drought_vol_pctile REAL")
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
        if 'wl_id' not in ptl_cols:
            # paper_positions already has wl_id; paper_trade_log never did -- harmless while
            # every ticker had at most one paper node, but the 2026-08-05 daily-track design
            # creates a live-track/daily-track pair that's identical in every OTHER column here
            # (ticker/strategy/version/window/account) by construction. Without wl_id, every
            # reader that queries this table by ticker (paper_vs_backtest_reconcile.py,
            # paper_trading_status.py, paper_signal_intrabar_check.py) silently merges both
            # tracks' trades into one stream (found by both Opus review passes, 2026-08-05).
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN wl_id INTEGER REFERENCES watch_list(id)")
        if 'signal_bar_time' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN signal_bar_time TEXT")
        if 'exit_bar_time' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN exit_bar_time TEXT")
        if 'position_source' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN position_source TEXT NOT NULL DEFAULT 'core' "
                      "CHECK (position_source IN ('core', 'drought_overlay'))")
        if 'drought_confirm_days' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN drought_confirm_days INTEGER")
        if 'drought_gap_start' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN drought_gap_start TEXT")
        if 'drought_vol_pctile' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN drought_vol_pctile REAL")
        if 'drought_vol_gate' not in ptl_cols:
            c.execute("ALTER TABLE paper_trade_log ADD COLUMN drought_vol_gate REAL")
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
        # daily_track_reconciliation_log -- one row per daily-track node per nightly
        # reconcile (paper_trading.reconcile_daily_track_nodes), full diagnostic snapshot
        # of both sides (not just a match/mismatch boolean -- user's explicit call
        # 2026-08-05: "logs would be terrible to query", and a bare verdict throws away
        # exactly the interesting part, e.g. how close daily-track's z was to the
        # backtest's even on a match). bar_match: whether the two sides agree on
        # flat-vs-open AND, if both open, the exact same entry bar. explained_by_price:
        # NULL if bar_match (nothing to explain) or uncomputable, else the counterfactual
        # re-check's verdict. action is descriptive, not imperative -- reconcile never
        # acts on any of these ('match', 'pending_skip', 'entry_miss_explained'/
        # 'unexplained', 'exit_wick_explained'/'unexplained', 'exit_bar_mismatch',
        # 'exit_early', 'ambiguous_position', 'no_backtest_data').
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_track_reconciliation_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL DEFAULT (datetime('now')),
                wl_id               INTEGER NOT NULL REFERENCES watch_list(id),
                ticker              TEXT NOT NULL,
                check_date          TEXT NOT NULL,
                actual_state        TEXT NOT NULL,
                actual_entry_price  REAL,
                actual_entry_time   TEXT,
                actual_signal_price REAL,
                backtest_state      TEXT NOT NULL,
                backtest_entry_price REAL,
                backtest_entry_time TEXT,
                backtest_signal_z   REAL,
                backtest_lower_band REAL,
                bar_match           INTEGER NOT NULL,
                explained_by_price  INTEGER,
                action              TEXT NOT NULL,
                detail              TEXT,
                actual_exit_time    TEXT,
                backtest_exit_time  TEXT
            )
        """)
        dtrl_cols = {r[1] for r in c.execute("PRAGMA table_info(daily_track_reconciliation_log)").fetchall()}
        if 'actual_exit_time' not in dtrl_cols:
            # Added 2026-08-05 alongside exit-side reconcile coverage -- exit comparisons
            # (same-bar-exit check once entries already match) need their own queryable
            # columns, not just prose in detail -- same "logs would be terrible to query"
            # principle the whole table exists for.
            c.execute("ALTER TABLE daily_track_reconciliation_log ADD COLUMN actual_exit_time TEXT")
        if 'backtest_exit_time' not in dtrl_cols:
            c.execute("ALTER TABLE daily_track_reconciliation_log ADD COLUMN backtest_exit_time TEXT")
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

        # skim_reserve_pool -- ONE pooled real SPY holding per account (not per node),
        # per docs/backlog_cache.md's 2026-08-04 "later" note: multiple live nodes in
        # the same account (e.g. HIBL/USD/YANG all in soxl_ira) skim into the same
        # physical SPY position, so shares aren't segregated -- watch_list.
        # skim_reserve_balance (per-node dollar ledger) tracks "whose" against this
        # single shared position. avg_cost is a blended average-cost basis (accepted
        # tradeoff -- fine for parking gains, not tax-lot precision; must stay
        # isolated from AGQ's own Section-1256/K-1 basis treatment, never blended
        # with it).
        # reserve_ticker/reserve_shares (not spy_shares) -- SPY is the current
        # validated parking vehicle (tested against QQQ/SSO/UPRO/QLD/TQQQ), but that
        # was a real research choice, not a hardcoded assumption; naming the column
        # after today's winner would make a future re-test look like a schema change
        # (Opus review, 2026-08-09). Composite PK + explicit NOT NULL on account --
        # a plain `account TEXT PRIMARY KEY` still permits multiple NULL rows in
        # SQLite, and watch_list.account is known-nullable in production
        # (signals_invariants.py has a dedicated check for it).
        # KNOWN OPEN GAP (not fixed here, flagged by review): nothing currently
        # guarantees two different ACCOUNTS nicknames (schwab_safety.py) can't
        # resolve to the same real brokerage account hash, which would silently
        # split one physical SPY holding across two pool rows here. Needs a
        # signals_invariants.py check before this pool is used for real capital.
        c.execute("""
            CREATE TABLE IF NOT EXISTS skim_reserve_pool (
                account        TEXT NOT NULL,
                reserve_ticker TEXT NOT NULL DEFAULT 'SPY',
                reserve_shares REAL NOT NULL DEFAULT 0,
                avg_cost       REAL,
                updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (account, reserve_ticker)
            )
        """)
        # skim_reserve_log -- one row per real skim/redeploy event, mirrors
        # watch_list_audit's mechanical-log pattern (append-only, never mutated).
        # ledger_balance_after is this node's skim_reserve_balance immediately after
        # the event, for a queryable running history without replaying every event.
        # No uniqueness constraint here deliberately -- the actual fire-once
        # guarantee lives at the state-transition level (watch_list.skim_ref
        # ratchets monotonically, skim_declining/skim_alert_sent_at gate the
        # redeploy alert), same pattern as pending_buys.gap_resize_date; this table
        # is pure append-only history, not itself a dedup mechanism.
        c.execute("""
            CREATE TABLE IF NOT EXISTS skim_reserve_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                   TEXT NOT NULL DEFAULT (datetime('now')),
                wl_id                INTEGER NOT NULL REFERENCES watch_list(id),
                account              TEXT NOT NULL,
                action               TEXT NOT NULL,  -- 'skim' | 'redeploy_alert' | 'redeploy'
                amount               REAL NOT NULL,
                reserve_shares_delta REAL,
                reserve_price        REAL,
                ledger_balance_after REAL NOT NULL,
                reference_value      REAL,
                detail               TEXT
            )
        """)
        # overlay_reconciliation_log -- pure-observation nightly diagnostic for the
        # drought/addon/skim mechanisms, mirroring daily_track_reconciliation_log's
        # shape exactly (docs/design.md's 2026-08-07 "Paper/live-vs-backtest
        # reconciliation" section -- item 3.5 of the staged checklist). One row per
        # node per mechanism per mode per night. Never mutates any real state --
        # same reasoning as the core daily-track reconcile: auto-correcting
        # divergence erases exactly the signal this comparison exists to produce.
        # mode + the UNIQUE constraint were both added on Opus review (2026-08-09):
        # without mode, drought/skim running on both live-track and daily-track
        # paper can't be told apart in the Grid; without the UNIQUE, an EOD
        # catch-up re-run after a restart silently duplicates the night's row
        # instead of upserting (the same gap daily_track_reconciliation_log has,
        # not inherited here on purpose).
        c.execute("""
            CREATE TABLE IF NOT EXISTS overlay_reconciliation_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT NOT NULL DEFAULT (datetime('now')),
                wl_id          INTEGER NOT NULL REFERENCES watch_list(id),
                ticker         TEXT NOT NULL,
                mechanism      TEXT NOT NULL,  -- 'drought' | 'addon' | 'skim'
                mode           TEXT NOT NULL,  -- 'live' | 'paper' | 'daily_sync'
                check_date     TEXT NOT NULL,
                actual_state   TEXT NOT NULL,
                backtest_state TEXT NOT NULL,
                match          INTEGER NOT NULL,
                action         TEXT NOT NULL,
                detail         TEXT,
                UNIQUE(wl_id, mechanism, mode, check_date)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_overlay_reconcile_wl_date "
                  "ON overlay_reconciliation_log(wl_id, check_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_open_positions_source "
                  "ON open_positions(position_source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_paper_positions_source "
                  "ON paper_positions(position_source)")

        # addon_legs/paper_addon_legs -- margin add-on-at-arm legs, DELIBERATELY a
        # separate table from open_positions/trade_log rather than a
        # position_source value there (see open_positions.position_source's
        # comment above for why: an addon leg shares its parent's ticker/wl_id and
        # opens WHILE the parent is still open, which would break every
        # single-row-per-ticker/wl_id assumption in this file -- get_open_position,
        # top_up_position, set_broker_stop_price, set_sl_order_id,
        # get_held_tickers, closed_today, and schwab_safety.check_order's
        # double-buy guard all assume at most one row). An addon leg never
        # independently triggers its own SL/TRAIL check -- it closes in lockstep
        # with its parent core position, driven by that position's own exit event,
        # never polled on its own. parent_trade_log_id (not parent_position_id) is
        # the PERMANENT link -- open_positions rows are deleted on close, so a
        # pointer to one would dangle once both legs finish; open_positions.
        # trade_log_id already gives the parent's permanent trade_log.id at leg
        # creation time, so that's captured directly instead.
        c.execute("""
            CREATE TABLE IF NOT EXISTS addon_legs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                wl_id                INTEGER NOT NULL REFERENCES watch_list(id),
                parent_position_id   INTEGER,
                parent_trade_log_id  INTEGER NOT NULL,
                ticker               TEXT NOT NULL,
                account              TEXT,
                shares               REAL NOT NULL,
                entry_price          REAL NOT NULL,
                entry_time           TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                exit_price           REAL,
                exit_time            TEXT,
                exit_reason          TEXT,
                pnl_pct              REAL,
                is_dry_run_sim       INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_addon_legs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                wl_id                INTEGER NOT NULL REFERENCES watch_list(id),
                parent_position_id   INTEGER,
                parent_trade_log_id  INTEGER NOT NULL,
                ticker               TEXT NOT NULL,
                account              TEXT,
                shares               REAL NOT NULL,
                entry_price          REAL NOT NULL,
                entry_time           TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                exit_price           REAL,
                exit_time            TEXT,
                exit_reason          TEXT,
                pnl_pct              REAL,
                is_dry_run_sim       INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_addon_legs_parent ON addon_legs(parent_position_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_paper_addon_legs_parent ON paper_addon_legs(parent_position_id)")

        # Real-execution-only columns on addon_legs -- deliberately NOT added to
        # paper_addon_legs, so the schema asymmetry itself documents the real/paper
        # difference (paper never places a broker order, see paper_trading.py's
        # own docstring). entry_status is a separate nullable column rather than
        # widening addon_legs.status's CHECK, to avoid a rebuild migration on a
        # live table (docs/plans/real_order_execution_drought_addon.md 0.10/2.2).
        al_cols = {r[1] for r in c.execute("PRAGMA table_info(addon_legs)").fetchall()}
        if 'entry_order_id' not in al_cols:
            c.execute("ALTER TABLE addon_legs ADD COLUMN entry_order_id INTEGER")
        if 'exit_order_id' not in al_cols:
            c.execute("ALTER TABLE addon_legs ADD COLUMN exit_order_id INTEGER")
        if 'sl_order_id' not in al_cols:
            c.execute("ALTER TABLE addon_legs ADD COLUMN sl_order_id INTEGER")
        if 'broker_stop_price' not in al_cols:
            c.execute("ALTER TABLE addon_legs ADD COLUMN broker_stop_price REAL")
        if 'entry_status' not in al_cols:
            # No CHECK constraint (ALTER TABLE ADD COLUMN limitation, same as
            # pending_buys.position_source above) -- enforced in
            # set_addon_leg_entry_filled/open_addon_leg instead. NULL for every
            # pre-existing (paper) row; real rows always set this explicitly.
            c.execute("ALTER TABLE addon_legs ADD COLUMN entry_status TEXT")
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

        # daily_plan: the EOD scenario review's "what do we expect to happen
        # tomorrow" snapshot, built fresh each EOD (2026-08-01) so the next
        # day's review has something concrete to diff real activity against,
        # across all three tiers (canary/live/paper) -- not just canary, which
        # scenario_expectations already covers on its own via a fixed, static
        # definition. category='canary' rows are a same-day copy of the
        # relevant scenario_expectations row (kept here too, not just
        # referenced by id, so a plan is a frozen point-in-time snapshot even
        # if scenario_expectations changes later); category='live'/'paper'
        # rows are derived from whatever position is open at plan-build time
        # (its real fixed_sl/arm_sell_pct/trail_sell_pct/max_hold_hours), so
        # they're only as good as "what's open right now" -- a position that
        # opens fresh tomorrow has no plan row for tomorrow, by construction.
        # No UNIQUE constraint -- same reasoning as scenario_expectations
        # (nullable ticker/node_id), dedup is the caller's job (clear_daily_plan
        # before rebuilding, not an upsert).
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_plan (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date         TEXT NOT NULL,
                category          TEXT NOT NULL,
                ticker            TEXT,
                node_id           INTEGER REFERENCES watch_list(id),
                expected_outcome  TEXT NOT NULL,
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        c.commit()

        # staged_test_config: the "what should this staged live test's config be"
        # mapping, structured instead of re-explained in conversation every time
        # (found needed live 2026-07-29 -- SH/RETL/GDXU's staged-test setup got
        # verbally re-derived and hand-checked repeatedly across one long
        # session instead of being queryable). One row per node currently
        # serving a deliberate live-test role; expected_config is a JSON dict
        # of the fields that role requires (e.g. {"arm_sell_pct": 0.3,
        # "fixed_sl": 50, "trail_sell_pct": 50, "max_hold_hours": 31}) --
        # scripts/audit_live_test_candidates.py's --staged flag diffs this
        # against the real current node/position config, the same MISMATCH
        # pattern already used there for fixed_sl-vs-real-order-price. UNIQUE
        # on wl_id is safe here (unlike scenario_expectations) since wl_id is
        # NOT NULL -- no nullable-column dedup gotcha.
        c.execute("""
            CREATE TABLE IF NOT EXISTS staged_test_config (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wl_id           INTEGER NOT NULL REFERENCES watch_list(id),
                ticker          TEXT NOT NULL,
                scenario_role   TEXT NOT NULL,
                expected_config TEXT NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE(wl_id)
            )
        """)
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

        # node_json 'state' backfill -- a pending_buys/paper_pending_buys row created
        # before the mode+dry_run -> state collapse (2026-08-06) has a node_json
        # snapshot with no 'state' key at all. Readers disagree on what a missing key
        # means: effectively_dry_run() treats it as not-live (fails closed, simulates),
        # but node_dry_run=(node.get('state') != 'live') at the real order-placement
        # call sites ALSO now fails closed post-fix -- but check_entry_abandon's cancel
        # branch (_effectively_dry_run gate) would still wrongly skip a real
        # cancel_order for an old row missing 'state' on a real node, since it reads
        # True (dry-run) for that case. Backfill from the live watch_list row (via the
        # wl_id just set above) so old snapshots carry the real state going forward --
        # matches the node's state AT THE TIME OF THIS MIGRATION, not necessarily at
        # order-placement time, which is the same best-effort limitation the wl_id
        # backfill above already accepts.
        for tbl in ('pending_buys', 'paper_pending_buys'):
            rows = c.execute(f"SELECT id, node_json, wl_id FROM {tbl}").fetchall()
            for r in rows:
                try:
                    node = json.loads(r['node_json'])
                except (TypeError, ValueError):
                    continue
                if 'state' in node or r['wl_id'] is None:
                    continue
                wl_row = c.execute("SELECT state FROM watch_list WHERE id=?", (r['wl_id'],)).fetchone()
                if wl_row is None:
                    continue
                node['state'] = wl_row['state']
                c.execute(f"UPDATE {tbl} SET node_json=? WHERE id=?", (json.dumps(node), r['id']))
        c.commit()

        # open_positions/paper_positions/trade_log/paper_trade_log carry no node_json to
        # recover wl_id from -- best-effort match each still-NULL row to a current
        # watch_list row on (ticker, strategy, version, window, account). An
        # unmatched/ambiguous row is left NULL (acceptable: a legacy position/trade from
        # a now-changed/deleted node, or a genuine live-track/daily-track duplicate --
        # not a live one) -- every row written via open_position()/log_trade_entry from
        # here forward always gets a real wl_id at insert time. trade_log/paper_trade_log
        # added 2026-08-1x: log_trade_entry's wl_id write only landed in commit fb699cf
        # (2026-08-09), so rows predating it were stuck NULL despite the sibling
        # open_positions/paper_positions row (much older write, since 2026-07-26) having
        # the right value all along -- confirmed real via coverage_events, not a live bug.
        for tbl in ('open_positions', 'paper_positions', 'trade_log', 'paper_trade_log'):
            rows = c.execute(
                f"SELECT id, ticker, strategy, version, window, account, entry_time FROM {tbl} WHERE wl_id IS NULL"
            ).fetchall()
            for r in rows:
                candidates = c.execute(
                    "SELECT id, added_at FROM watch_list WHERE ticker=? AND strategy=? AND version=? "
                    "AND window=? AND COALESCE(account,'')=COALESCE(?,'')",
                    (r['ticker'], r['strategy'], r['version'], r['window'], r['account']),
                ).fetchall()
                if r['entry_time']:
                    # A trade that predates a candidate node's own creation can't
                    # belong to it -- applies regardless of candidate count.
                    # CRITICAL fix (found by paired Opus review, 2026-08-1x): this
                    # filter originally only ran when len(candidates) > 1, added to
                    # break a live-track/daily-track tie (added 2026-08-05, both
                    # identical on every column above except added_at). But a
                    # SINGLE candidate can be just as wrong -- confirmed live via
                    # watch_list_audit (added_at is never reset by a rebuild
                    # migration, it's the real add_node timestamp): 8 real trade_log
                    # rows had been silently attributed to a since-deleted-and-
                    # recreated node (e.g. a 'soxl_test'/'canary' version reused
                    # after the original was removed) whose real creation postdated
                    # the trade by hours to days. Applying this filter universally
                    # correctly nulls those back out -- same "acceptable, leave
                    # NULL" convention this migration already uses for a genuinely
                    # unmatchable row, not a regression.
                    candidates = [cand for cand in candidates if cand['added_at'] <= r['entry_time']]
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
    canary/test node not on the currently-active watchlist).

    Always excludes paper_role='daily_sync' rows (the 2026-08-05 daily-track
    clones) -- without this, every v5 ticker with a daily-track sibling
    (same ticker/account/mode='research' as its live-track node) makes this
    lookup ambiguous for BOTH nodes even when only one was actually meant,
    including at real callers like schwab_safety.py's node-level circuit
    breaker (found by paired Opus review, 2026-08-05). A daily-track node is
    never the right match for a generic ticker+disambiguator lookup -- it's
    only ever addressed by its explicit wl_id."""
    try:
        if watchlist_id is None:
            watchlist_id = get_active_watchlist_id()
        q = "SELECT * FROM watch_list WHERE ticker = ? AND paper_role IS NULL"
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
             label='', z_score_threshold=2.0, watchlist_id=None, state='paper',
             trail_buy_pct=None, trail_pct=None, entry_timing='close', starting_notional=50000,
             fixed_sl_override=None, account=None, paper_role=None):
    """trail_buy_pct/trail_pct: pass the real values directly for v3.x nodes (where
    backtest_cache has real named columns). Omit both for legacy v1.x/v2.x nodes —
    falls back to reinterpreting stop_loss the way it's always meant for the 4
    trailing strategies (see docs/design.md 'Grid axis meaning by strategy').
    For v3.x trailing-both/trailing-exit nodes, the stop_loss arg is not a real
    swept value (backtest_cache stores config.execution.fixed_stop_loss there,
    a constant) — pass whatever backtest_cache's stop_loss column shows, it's vestigial.
    fixed_sl_override: pass the real per-node SL (e.g. a v4 SL-sweep value) directly —
    without it, uses_fixed_sl strategies always fall back to config.json's stale global
    default, which is wrong for any node whose real SL differs from that default.
    state: 'paper' / 'dry_run' / 'live' (see ensure_tables()'s schema comment) —
    replaces the old separate mode='live'/'research' + node-level dry_run override."""
    if state not in ('paper', 'dry_run', 'live'):
        raise ValueError(f"add_node: invalid state {state!r} -- must be 'paper'/'dry_run'/'live'")
    if state != 'paper' and account and ticker.upper() in TAX_ADVANTAGED_EXCLUDED_TICKERS and any(
            kw in account.lower() for kw in ("ira", "roth", "sep")):
        raise ValueError(
            f"add_node refused: {ticker} is excluded from tax-advantaged accounts "
            f"(account={account!r}) — see CLAUDE.md's 'Ticker exclusion, decided "
            f"2026-08-04' note (K-1/UBTI risk). Confirm the ticker's real K-1 status "
            f"before overriding.")
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
              AND COALESCE(paper_role, '') = COALESCE(?, '')
        """, (watchlist_id, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours),
              stored_arm_sell_pct, stored_trail_buy_pct, stored_trail_sell_pct, account,
              paper_role)).fetchone()
        if existing:
            return
        cur = c.execute("""
            INSERT INTO watch_list
                (watchlist_id, state, ticker, strategy, version, window, take_profit,
                 stop_loss, max_hold_hours, label, z_score_threshold, trail_sell_pct, fixed_sl,
                 trail_buy_pct, arm_sell_pct, entry_timing, starting_notional, account, paper_role)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watchlist_id, state, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours), label, float(z_score_threshold),
              stored_trail_sell_pct, fixed_sl, stored_trail_buy_pct, stored_arm_sell_pct,
              entry_timing, float(starting_notional), account, paper_role))
        _log_audit(c, 'add_node', watchlist_id=watchlist_id, watch_id=cur.lastrowid,
                   ticker=ticker, detail=f"strategy={strategy} version={version} state={state}")
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


def set_node_state(watch_id, state):
    """Sets the one field answering "what is this node doing right now" --
    'paper' / 'dry_run' / 'live' (collapses the old separate set_node_mode +
    set_node_dry_run, 2026-08-1x). add_node's tax-advantaged-account guard
    only fires at node CREATION time (state != 'paper' + an excluded ticker +
    an ira/roth/sep account) -- a node created state='paper' in that same
    account, then flipped to dry_run/live via this function, bypassed it
    entirely (found by paired Opus review, 2026-08-05, for the old mode
    version of this same gap). Mirrors add_node's exact check rather than
    re-deriving it. Real order placement additionally requires the
    account-level schwab_safety.ACCOUNTS[account].trading_enabled ceiling --
    this function only ever sets the node's own half of that AND."""
    if state not in ('paper', 'dry_run', 'live'):
        raise ValueError(f"set_node_state: invalid state {state!r} -- must be 'paper'/'dry_run'/'live'")
    with _conn() as c:
        row = c.execute(
            "SELECT watchlist_id, ticker, state, account FROM watch_list WHERE id = ?", (watch_id,)
        ).fetchone()
        if row and state != 'paper' and row['account'] and row['ticker'].upper() in TAX_ADVANTAGED_EXCLUDED_TICKERS and any(
                kw in row['account'].lower() for kw in ("ira", "roth", "sep")):
            raise ValueError(
                f"set_node_state refused: {row['ticker']} (wl_id={watch_id}) is excluded from "
                f"tax-advantaged accounts (account={row['account']!r}) -- see CLAUDE.md's "
                f"'Ticker exclusion, decided 2026-08-04' note (K-1/UBTI risk). Confirm the "
                f"ticker's real K-1 status before overriding.")
        c.execute("UPDATE watch_list SET state = ? WHERE id = ?", (state, watch_id))
        if row:
            _log_audit(c, 'set_node_state', watchlist_id=row['watchlist_id'], watch_id=watch_id,
                       ticker=row['ticker'], detail=f"{row['state']} -> {state}")
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


def dup_alert_suppressed_today(node_id):
    """True if dup_buy_alert_suppressed already fired for this node today (local
    time -- same date(ts,'localtime') pattern used elsewhere to avoid the
    UTC-vs-ET offset bug class). Throttles the repeat Slack suppression message
    to once/day for a node whose broker-truth block never clears (e.g. an
    unrelated manual order/holding) without changing the block itself -- the
    dedupe check keeps re-evaluating broker truth every poll regardless."""
    with _conn() as c:
        row = c.execute("""
            SELECT 1 FROM coverage_events
            WHERE node_id = ? AND scenario_key = 'dup_buy_alert_suppressed'
              AND date(ts, 'localtime') = date('now', 'localtime')
            LIMIT 1
        """, (node_id,)).fetchone()
    return row is not None


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
    expected_frequency: 'daily' (checked every trading day, a miss is a real
    ticket needing a human) / 'informational' (checked and printed every
    trading day same as 'daily', but a miss never records a coverage_deviations
    ticket -- for a scenario whose underlying trigger condition is itself
    trade-conditional, so "didn't happen today" is expected some days, not a
    failure -- e.g. canary_pinned_entry/canary_time_exit/reconciliation_mismatch,
    2026-07-30) / 'occasional' (not checked by the daily cron at all) /
    'regression-only'.
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


def set_staged_test_config(wl_id, ticker, scenario_role, expected_config: dict, notes=None):
    """Insert-or-update the structured 'what should this staged live test's
    config be' row for a node -- see ensure_tables' staged_test_config
    comment. expected_config is stored as JSON; pass a plain dict."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    config_json = json.dumps(expected_config)
    with _conn() as c:
        existing = c.execute("SELECT id FROM staged_test_config WHERE wl_id = ?", (wl_id,)).fetchone()
        if existing:
            c.execute("""
                UPDATE staged_test_config SET
                    ticker=?, scenario_role=?, expected_config=?, notes=?, updated_at=?
                WHERE wl_id=?
            """, (ticker, scenario_role, config_json, notes, now_str, wl_id))
        else:
            c.execute("""
                INSERT INTO staged_test_config
                    (wl_id, ticker, scenario_role, expected_config, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (wl_id, ticker, scenario_role, config_json, notes, now_str, now_str))
        c.commit()


def get_staged_test_configs():
    """Every currently-active staged-live-test row, expected_config parsed
    back into a dict."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM staged_test_config ORDER BY id").fetchall()]
    for r in rows:
        r['expected_config'] = json.loads(r['expected_config'])
    return rows


def clear_staged_test_config(wl_id):
    """Removes a node's staged-test row once that test has concluded (fixed
    and confirmed live, or abandoned) -- not meant to accumulate forever."""
    with _conn() as c:
        c.execute("DELETE FROM staged_test_config WHERE wl_id = ?", (wl_id,))
        c.commit()


def add_daily_plan_row(plan_date, category, expected_outcome, ticker=None, node_id=None):
    with _conn() as c:
        c.execute("""
            INSERT INTO daily_plan (plan_date, category, ticker, node_id, expected_outcome)
            VALUES (?, ?, ?, ?, ?)
        """, (plan_date, category, ticker, node_id, expected_outcome))
        c.commit()


def get_daily_plan(plan_date, category=None):
    q = "SELECT * FROM daily_plan WHERE plan_date = ?"
    params = [plan_date]
    if category:
        q += " AND category = ?"
        params.append(category)
    q += " ORDER BY category, ticker"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def clear_daily_plan(plan_date):
    """Called before rebuilding a day's plan so a re-run (e.g. daemon restart
    after the EOD slot) replaces rather than duplicates that day's rows."""
    with _conn() as c:
        c.execute("DELETE FROM daily_plan WHERE plan_date = ?", (plan_date,))
        c.commit()


def get_scenario_expectations(expected_frequency=None, active_only=True):
    """expected_frequency accepts a single value or a list/tuple of values."""
    q = "SELECT * FROM scenario_expectations"
    clauses, params = [], []
    if expected_frequency:
        if isinstance(expected_frequency, (list, tuple)):
            clauses.append(f"expected_frequency IN ({','.join('?' * len(expected_frequency))})")
            params.extend(expected_frequency)
        else:
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
    upsert rather than a UNIQUE-backed ON CONFLICT.

    Returns the row's id (2026-08-08, needed by coverage_check.py's price-action
    auto-explain: it must immediately call explain_deviation on this exact row right
    after recording it, not rely on a later re-check)."""
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
                    SET expected_outcome=?, actual_summary=?, ts=datetime('now'), reason=NULL, reason_by=NULL, reason_ts=NULL
                    WHERE id=?
                """, (expected_outcome, actual_summary, existing['id']))
            else:
                c.execute("""
                    UPDATE coverage_deviations SET expected_outcome=?, actual_summary=?, ts=datetime('now')
                    WHERE id=?
                """, (expected_outcome, actual_summary, existing['id']))
            c.commit()
            return existing['id']
        else:
            cur = c.execute("""
                INSERT INTO coverage_deviations
                    (check_date, scenario_key, ticker, node_id, mode, expected_outcome, actual_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (check_date, scenario_key, ticker, node_id, mode, expected_outcome, actual_summary))
            c.commit()
            return cur.lastrowid


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
    # exit_reason='RESTAGED' (scripts/restage_canary_nodes.py, 2026-08-08) is a
    # maintenance close, not a real market-driven outcome -- excluded here so a
    # same-day entry that gets restaged that evening can't be picked up as
    # "the" scenario trade for that date (would report a spurious "expected
    # TP/TRAIL, got RESTAGED" deviation and, via record_deviation's clear-on-
    # change logic, could silently overwrite an already-correct explanation).
    q = ("SELECT * FROM trade_log WHERE ticker = ? AND date(entry_time) = ? AND date(exit_time) = ? "
         "AND (exit_reason IS NULL OR exit_reason != 'RESTAGED')")
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


def get_open_positions_for_ticker_on_date(ticker, check_date, strategy=None, version=None,
                                           window=None, account=None):
    """open_positions rows entered on or before check_date and still open --
    the third real state (pending -> open -> closed) coverage_check.py's
    trade_lifecycle carryover check was missing until 2026-08-10 (SDOW's
    real canary fill was still open, not yet closed, when the daily check
    ran -- neither get_pending_buys_for_ticker_on_date (already resolved,
    row deleted on fill) nor get_closed_trades_for_ticker_on_date (not
    exited yet) saw it, so the check reported false 'no activity' -- the 3rd
    independent recurrence of this exact bug shape, see docs/
    backlog_cache.md's 2026-08-04 'broader diagnosis' item).

    date(entry_time) <= check_date, not == -- a paired-review finding on the
    first version of this function (2026-08-10): an exact-date match
    reproduces the identical bug on day 2 of a multi-day-open position (the
    row is still present, still real activity, but entry_time is no longer
    "today"). This mirrors get_pending_buys_for_ticker_on_date's own
    signal_time <= check_date fix for the identical reasoning -- see that
    function's docstring. open_positions has no exit_time column --
    presence in the table means currently open by construction
    (close_position deletes the row and writes trade_log instead), so no
    exit_time filter is needed the way the closed-trades sibling needs one.
    Scoped the same way as its two siblings above."""
    q = "SELECT * FROM open_positions WHERE ticker = ? AND date(entry_time) <= ?"
    params = [ticker, check_date]
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
    filter after the ticker/date SQL match, not a SQL WHERE clause.

    signal_time <= check_date (not ==): a pending_buys row is deleted the
    moment it resolves (clear_pending_buy*), so any row still present as of
    check_date is still genuinely resting, regardless of which day it was
    created -- a carryover scenario (e.g. canary_overnight_carry) is
    specifically one whose signal_time is a PRIOR day. An exact-date match
    produced a false "no pending_buys row" for every such scenario once the
    order aged past its signal day (found 2026-08-04)."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT * FROM pending_buys WHERE ticker = ? AND date(signal_time) <= ?
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


def get_trades_closed_on_date(check_date, paper=False):
    """All trade_log/paper_trade_log rows that exited on check_date (YYYY-MM-DD),
    across every ticker -- the all-tickers counterpart to
    get_closed_trades_for_ticker_on_date, used by the EOD scenario review to
    summarize the whole day's live/paper activity, not one ticker at a time."""
    _, trades_table = _pos_tables(paper)
    with _conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {trades_table} WHERE date(exit_time) = ? ORDER BY exit_time", (check_date,)
        ).fetchall()]


def get_trades_opened_on_date(check_date, paper=False):
    """Same shape as get_trades_closed_on_date but keyed off entry_time --
    used to catch same-day entries that are still open (no exit yet)."""
    _, trades_table = _pos_tables(paper)
    with _conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {trades_table} WHERE date(entry_time) = ? ORDER BY entry_time", (check_date,)
        ).fetchall()]


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


def get_real_open_position(ticker, account=None):
    """Real (is_dry_run_sim=0) open_positions row only, optionally scoped to
    account -- neither get_open_position nor get_open_position_for_account
    exclude synthetic dry-run-sim rows, which is wrong for a caller
    specifically asking "does a REAL position already exist" (found 2026-08-07:
    _reconcile_buy_fill's orphan-fill check used bare get_open_position(ticker),
    so a same-ticker is_dry_run_sim=1 canary position in a different account
    would be mistaken for "this real fill was already reconciled" and silently
    suppress the alert for a genuinely untracked real fill)."""
    with _conn() as c:
        if account:
            row = c.execute(
                "SELECT * FROM open_positions WHERE ticker=? AND account=? AND is_dry_run_sim=0 "
                "ORDER BY entry_time DESC LIMIT 1", (ticker, account)
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM open_positions WHERE ticker=? AND is_dry_run_sim=0 "
                "ORDER BY entry_time DESC LIMIT 1", (ticker,)
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


def get_orphaned_open_position_for_account(ticker, account, paper=False):
    """Sibling of get_open_position_for_account, scoped to wl_id IS NULL rows
    only -- a legacy position predating the wl_id migration, or one the
    backfill couldn't uniquely resolve (see open_position()'s own matching
    comment). Added 2026-08-10 as a safety-net fallback for check_order's
    node_id-scoped BUY/SELL guards: get_open_position_by_wl_id(node_id) can't
    see an orphaned row (it has no wl_id to match), and returning None there
    must not be read as "this ticker+account is genuinely flat" -- real
    capital could still be sitting in an unattributed row. Deliberately does
    NOT fall back further to any wl_id-tagged row (that would reintroduce the
    exact sibling-misattribution bug node_id threading was built to fix) --
    only a truly unattributed position counts here."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {positions_table} WHERE ticker=? AND account=? AND wl_id IS NULL "
            f"ORDER BY entry_time DESC LIMIT 1",
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


def get_real_position_state(wl_id):
    """Single node-scoped "what's actually going on with this node right now" call,
    replacing the hand-rolled ticker-keyed state-assembly each of status_check.py/
    audit_live_test_candidates.py and watchlist_status.py used to do independently.
    That duplication is what let the same bug class (pending_buys invisible to
    "flat" classification) exist in one of the two scripts after being fixed in the
    other -- see docs/deep_backlog.md's 2026-08-04/2026-08-05 entries.

    Deliberately wl_id-scoped, not ticker-scoped -- ticker alone is ambiguous once
    2+ concurrent nodes share a ticker (the same root cause behind the wl_id
    refactor). Callers that only have a ticker are still responsible for resolving
    it to the right node first (e.g. audit_live_test_candidates._resolve_live_node);
    that resolution policy (which node "is" a ticker's live/paper node) is
    deliberately NOT folded in here, since it's a different concern from "given a
    specific node, what's its real state."

    Returns a dict: node, real_position, paper_position, pending_buy,
    paper_pending_buy, status. `status` is one of:
      'flat'          -- no position, no resting order, on either side
      'pending_entry' -- a resting trailing-buy (real or paper), not yet filled
      'holding'       -- a real open position
      'holding_paper' -- a paper open position (covers both live-track and
                         daily-track paper roles; see node['paper_role'] to
                         distinguish further if a caller needs to)
    A real and paper position/pending-buy are independent axes (a node can have
    at most one of each kind at a time in normal operation) -- status reports
    real over paper when both happen to be present, since real capital always
    takes precedence for a human reading the summary.
    """
    node = get_watch_list_node_by_id(wl_id)
    real_position = get_open_position_by_wl_id(wl_id, paper=False)
    paper_position = get_open_position_by_wl_id(wl_id, paper=True)
    pending_buy = get_pending_buy_by_wl_id(wl_id)
    paper_pending_buy = get_paper_pending_buy(wl_id)

    # real_position, then a REAL pending_buy, outrank anything paper -- a resting
    # real order is committed capital and must never be shadowed by a paper
    # position/pending-buy that happens to exist on the same node (the exact
    # "real state shadowed" failure this function exists to eliminate; found by
    # paired Opus review, 2026-08-05, contradicting this docstring's own rule).
    if real_position is not None:
        status = 'holding'
    elif pending_buy is not None:
        status = 'pending_entry'
    elif paper_position is not None:
        status = 'holding_paper'
    elif paper_pending_buy is not None:
        status = 'pending_entry'
    else:
        status = 'flat'

    return {
        'node': node,
        'real_position': real_position,
        'paper_position': paper_position,
        'pending_buy': pending_buy,
        'paper_pending_buy': paper_pending_buy,
        'status': status,
    }


def get_trade_log_for_wl_id(wl_id, paper=False, limit=50):
    """wl_id-scoped trade_log/paper_trade_log history -- added 2026-08-05 for
    reconcile_daily_track_nodes' classification logic (does this node have a
    closed trade at the same signal bar the backtest is currently referencing?
    used to distinguish 'flat, never entered' from 'flat, already handled').
    Newest first."""
    _, trades_table = _pos_tables(paper)
    with _conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {trades_table} WHERE wl_id = ? ORDER BY entry_time DESC LIMIT ?",
            (wl_id, limit)
        ).fetchall()]


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
                          'arm_sell_pct', 'account', 'starting_notional', 'state')


def add_pending_buy(node, sig, channel, ts, order_id=None, position_source='core',
                     drought_confirm_days=None, drought_vol_gate=None, drought_gap_start=None,
                     drought_vol_pctile=None):
    if position_source not in ('core', 'drought_overlay'):
        raise ValueError(f"invalid position_source: {position_source!r}")
    node_subset = {k: node.get(k) for k in _PENDING_BUY_NODE_KEYS}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_buys (ticker, node_json, signal_price, signal_time, "
            "reminder_channel, reminder_ts, reminder_count, last_reminder_at, created_at, order_id, wl_id, "
            "running_low, position_source, drought_confirm_days, drought_vol_gate, drought_gap_start, "
            "drought_vol_pctile) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), channel, ts, now_str, now_str, order_id,
             node.get('id'), sig['current_price'], position_source, drought_confirm_days,
             drought_vol_gate, drought_gap_start, drought_vol_pctile),
        )
        c.commit()


def get_drought_pending_buy(wl_id):
    """pending_buys counterpart to get_open_position_by_wl_id's drought filter --
    mirrors get_pending_buy_by_wl_id but scoped to position_source='drought_overlay',
    used by check_drought_handoff to distinguish a resting drought entry order
    from a resting core one for the same node."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM pending_buys WHERE wl_id = ? AND position_source = 'drought_overlay' "
            "ORDER BY id DESC LIMIT 1",
            (wl_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['node'] = json.loads(d['node_json'])
    return d


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
    """Mirrors update_paper_pending_buy_running_low. Called by both
    signals_notify.update_dry_run_buys (dry_run accounts) and
    update_real_pending_buys_running_low (real accounts, added 2026-07-29 --
    the broker tracks a real trailing-buy's running low itself, but
    check_gap_resize reads this column assuming it's live-tracked, so a real
    account needs its own copy updated too)."""
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


def get_pending_buy_by_wl_id(wl_id):
    """Node-scoped single-row lookup -- the pending_buys counterpart to
    get_open_position_by_wl_id, added 2026-08-05 so a still-resting trailing-buy
    doesn't render as flat/no-activity in state-summary tools that only ever
    checked open_positions (status_check.py/audit_live_test_candidates.py)."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM pending_buys WHERE wl_id = ? ORDER BY id DESC LIMIT 1",
                         (wl_id,)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r['node'] = json.loads(r['node_json'])
    return r


def log_daily_track_reconciliation(wl_id, ticker, check_date, actual_state, backtest_state, bar_match,
                                    action, actual_entry_price=None, actual_entry_time=None,
                                    actual_signal_price=None, backtest_entry_price=None,
                                    backtest_entry_time=None, backtest_signal_z=None,
                                    backtest_lower_band=None, explained_by_price=None, detail=None,
                                    actual_exit_time=None, backtest_exit_time=None):
    with _conn() as c:
        c.execute("""
            INSERT INTO daily_track_reconciliation_log
                (wl_id, ticker, check_date, actual_state, actual_entry_price, actual_entry_time,
                 actual_signal_price, backtest_state, backtest_entry_price, backtest_entry_time,
                 backtest_signal_z, backtest_lower_band, bar_match, explained_by_price, action, detail,
                 actual_exit_time, backtest_exit_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (wl_id, ticker, check_date, actual_state, actual_entry_price, actual_entry_time,
              actual_signal_price, backtest_state, backtest_entry_price, backtest_entry_time,
              backtest_signal_z, backtest_lower_band, 1 if bar_match else 0,
              None if explained_by_price is None else (1 if explained_by_price else 0), action, detail,
              actual_exit_time, backtest_exit_time))
        c.commit()


def get_daily_track_reconciliation_log(wl_id=None, limit=200):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        if wl_id is not None:
            rows = c.execute(
                "SELECT * FROM daily_track_reconciliation_log WHERE wl_id = ? ORDER BY id DESC LIMIT ?",
                (wl_id, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM daily_track_reconciliation_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_overlay_reconciliation(wl_id, ticker, mechanism, mode, check_date, actual_state,
                                backtest_state, match, action, detail=None):
    """One row per node per mechanism per mode per night -- mirrors
    log_daily_track_reconciliation's shape and pure-observation contract
    exactly (paper_trading.reconcile_overlay_nodes, 2026-08-09). INSERT OR
    REPLACE respects the table's UNIQUE(wl_id, mechanism, mode, check_date)
    constraint -- a same-day rerun (e.g. after a restart) replaces the
    night's row with the LATEST result rather than duplicating it. Was
    INSERT OR IGNORE (first-result-wins) until an independent review,
    2026-08-09, found that let a transient 'replay_failed' row from an early
    run permanently mask a later clean 'match' the same night -- REPLACE
    means a genuine improvement on retry actually sticks. Unlike
    daily_track_reconciliation_log, which has no UNIQUE constraint at all
    and duplicates on every rerun (a known, not-yet-fixed gap there, not
    inherited here)."""
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO overlay_reconciliation_log
                (wl_id, ticker, mechanism, mode, check_date, actual_state, backtest_state,
                 match, action, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (wl_id, ticker, mechanism, mode, check_date, actual_state, backtest_state,
              1 if match else 0, action, detail))
        c.commit()


def get_overlay_reconciliation_log(wl_id=None, mechanism=None, limit=200):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        q = "SELECT * FROM overlay_reconciliation_log WHERE 1=1"
        params = []
        if wl_id is not None:
            q += " AND wl_id=?"
            params.append(wl_id)
        if mechanism is not None:
            q += " AND mechanism=?"
            params.append(mechanism)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def set_daily_sync_halted(wl_id, halted=True):
    """Halts (or clears) a daily-track node's paper entry -- set when a nightly reconcile
    finds an unexplained divergence (see reconcile_daily_track_nodes' docstring). Clearing
    is a manual, deliberate action (no code path clears this automatically)."""
    with _conn() as c:
        c.execute("UPDATE watch_list SET daily_sync_halted_at = ? WHERE id = ?",
                   (datetime.now().strftime('%Y-%m-%d %H:%M:%S') if halted else None, wl_id))
        _log_audit(c, 'set_daily_sync_halted', watch_id=wl_id, detail=f"halted={halted}")
        c.commit()


def get_daily_track_bookmark(wl_id):
    """Returns the signal-bar timestamp (string) of the last backtest trade this
    daily-track node has been fully, conclusively reconciled through, or None if
    reconciliation hasn't started yet for this node."""
    with _conn() as c:
        row = c.execute("SELECT daily_track_bookmark_signal_bar FROM watch_list WHERE id = ?",
                         (wl_id,)).fetchone()
        return row[0] if row else None


def set_daily_track_bookmark(wl_id, signal_bar_str):
    """Advances (or clears, with None) the daily-track reconcile bookmark. Only
    called from reconcile_daily_track_nodes once a specific backtest trade's
    comparison reaches a terminal verdict -- never a position-state mutation,
    purely tracks which comparison is in progress. See the watch_list schema
    migration comment (ensure_tables) for the full rationale.

    Deliberately NOT logged via _log_audit (unlike set_daily_sync_halted,
    a real behavioral toggle) -- this is a diagnostic cursor advanced
    routinely, potentially several times per node per reconcile pass (see
    the "single-pass catch-up" note in reconcile_daily_track_nodes'
    docstring); logging every advance would dilute watch_list_audit, the
    mutation history of record for genuine node config changes, with noise
    (flagged by both the cold and contextual paired review, 2026-08-10)."""
    with _conn() as c:
        c.execute("UPDATE watch_list SET daily_track_bookmark_signal_bar = ? WHERE id = ?",
                   (signal_bar_str, wl_id))
        c.commit()


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
    pending row (see docs/backlog_cache.md's wl_id refactor entry).

    Parses node_json into a 'node' key, matching get_paper_pending_buys and
    get_pending_buy_by_wl_id -- this sibling function didn't do so, which
    crashed get_real_position_state's callers (watchlist_status.py) the
    moment a real paper_pending_buy row existed and got passed to
    _trailing_buy_status, whose first line reads pending['node'] (found by
    paired Opus review, 2026-08-05)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM paper_pending_buys WHERE wl_id = ?", (wl_id,)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r['node'] = json.loads(r['node_json'])
    return r


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
                   is_dry_run_sim=False, signal_bar_time=None, position_source='core',
                   drought_confirm_days=None, drought_vol_gate=None, drought_gap_start=None,
                   drought_vol_pctile=None):
    """Returns True if a position was opened, False if skipped because one was
    already open for this node — callers that report success to Slack
    must check this, since a silent skip must not be reported as a fill.
    Dedup keys on node['id'] (wl_id), not (ticker, window) -- two concurrent
    nodes could otherwise share a window and collide (see docs/backlog_cache.md's
    wl_id refactor entry).
    is_dry_run_sim tags a fill synthesized against real price data because the
    account is dry_run (no real broker fill will ever arrive) -- mutually
    exclusive with paper (a dry_run node is always mode='live', never research).
    position_source='drought_overlay' (with the drought_* fields) is the ONLY
    non-'core' value accepted here -- add-on legs use open_addon_leg() and their
    own dedicated table instead, never this function (see
    open_positions.position_source's schema comment for why: an addon leg shares
    its parent's wl_id and opens while the parent is still open, which this
    function's own dedup check, and most of this file's ticker/wl_id-keyed
    lookups, assume never happens)."""
    if position_source == 'addon_leg':
        raise ValueError("open_position() does not support position_source='addon_leg' -- use open_addon_leg()")
    positions_table, _ = _pos_tables(paper)
    with _position_lock, _conn() as c:
        # position_lock instrumentation (2026-08-01): proves the lock is
        # actually acquired around the check-then-insert below, not just that
        # the surrounding code runs -- a concurrency test can seed 2 threads
        # racing this same node and assert only one ever logs "opened" while
        # the other logs "skipped_duplicate", never both racing past the
        # existing-row check unlocked.
        log_coverage_event("position_lock", 'paper' if paper else ('dry_run' if is_dry_run_sim else 'live'),
                            ticker=node['ticker'], node_id=node.get('id'), result="acquired", detail="open_position")
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
            log_coverage_event("position_lock", 'paper' if paper else ('dry_run' if is_dry_run_sim else 'live'),
                                ticker=node['ticker'], node_id=node.get('id'), result="skipped_duplicate",
                                detail="open_position")
            return False
        sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
        entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
        signal_bar_time_str = (signal_bar_time.strftime('%Y-%m-%d %H:%M:%S')
                                if hasattr(signal_bar_time, 'strftime') else signal_bar_time)
        trade_log_id = log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares,
                                        paper=paper, is_dry_run_sim=is_dry_run_sim,
                                        signal_bar_time=signal_bar_time, position_source=position_source,
                                        drought_confirm_days=drought_confirm_days, drought_vol_gate=drought_vol_gate,
                                        drought_gap_start=drought_gap_start, drought_vol_pctile=drought_vol_pctile)
        tp = node.get('take_profit')
        c.execute(f"""
            INSERT INTO {positions_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, trade_log_id,
                 trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, shares, account, wl_id, is_dry_run_sim,
                 signal_bar_time, position_source, drought_confirm_days, drought_vol_gate,
                 drought_gap_start, drought_vol_pctile)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
            node.get('trail_sell_pct'), node.get('fixed_sl'), node.get('trail_buy_pct'),
            node.get('arm_sell_pct'), float(shares) if shares is not None else None,
            node.get('account'), node.get('id'), 1 if is_dry_run_sim else 0,
            signal_bar_time_str, position_source, drought_confirm_days, drought_vol_gate,
            drought_gap_start, drought_vol_pctile,
        ))
        c.commit()
        return True


def open_position_from_pending(pending, signal_price, signal_time, entry_price, entry_time, shares,
                                paper=False, is_dry_run_sim=False):
    """Single dispatch point for opening a position from a pending_buys row's
    position_source discriminator -- shared by every real fill consumer
    (signals_notify._reconcile_buy_fill, signals_handlers.handle_trail_buy_fill_price,
    signals_handlers.handle_entry_price) so the three sites can't
    independently drift, the exact drift pattern that produced the
    take_profit/trail_buy_pct column-overload bug found 3x in this
    codebase's history (docs/plans/real_order_execution_drought_addon.md 4.3).

    pending is a dict with 'node' + 'position_source' (+ drought_* fields)
    keys, as returned by get_pending_buy_by_wl_id/get_drought_pending_buy/
    get_pending_buys -- callers that only have a Slack-metadata node dict
    (not a fresh pending_buys row) must look one up first, e.g.
    get_pending_buy_by_wl_id(node['id']), BEFORE clearing it.

    signal_price/signal_time/entry_price/entry_time/shares are passed
    through faithfully (not re-derived here) -- the three real call sites
    have genuinely different signal_time semantics (fill-moment vs. the
    original backdated signal for a manual catch-up entry, see
    _reconcile_buy_fill's own hold-time-origin comment), so this function
    must not assume signal_time == entry_time."""
    node = pending['node']
    if pending.get('position_source') == 'drought_overlay':
        return open_position(
            node, signal_price, signal_time, entry_price, entry_time, shares=shares, paper=paper,
            is_dry_run_sim=is_dry_run_sim, position_source='drought_overlay',
            drought_confirm_days=pending.get('drought_confirm_days'),
            drought_vol_gate=pending.get('drought_vol_gate'),
            drought_gap_start=pending.get('drought_gap_start'),
            drought_vol_pctile=pending.get('drought_vol_pctile'),
        )
    return open_position(node, signal_price, signal_time, entry_price, entry_time, shares=shares,
                          paper=paper, is_dry_run_sim=is_dry_run_sim)


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


def set_broker_stop_price_by_position(position_id, broker_stop_price):
    """Position-id-scoped -- unlike the legacy ticker-keyed set_broker_stop_price
    (never actually called in production), which would misattribute the price
    to the wrong node's position whenever 2+ nodes share a ticker, the same bug
    class already fixed elsewhere via the wl_id refactor (see
    set_sl_order_id_by_position). broker_stop_price=None clears it -- must be
    called whenever the real stop it describes is replaced (an arm-time
    trailing-sell, a TP/SL/TIME market-sell exit), or stop_status() reports a
    stale 'known' price for a stop that no longer exists (Opus review,
    2026-08-01, found this live path: an SL alert would say "broker stop on
    file, no action needed" for a position whose stop was actually just
    replaced by an unconfirmed market sell)."""
    price = None if broker_stop_price is None else float(broker_stop_price)
    with _conn() as c:
        c.execute("UPDATE open_positions SET broker_stop_price = ? WHERE id = ?",
                   (price, position_id))
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


def close_position(position_id, exit_signal_price=None, exit_price=None, exit_time=None, exit_reason=None, paper=False,
                    exit_bar_time=None):
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
            f"SELECT trade_log_id, entry_price, ticker, wl_id, is_dry_run_sim FROM {positions_table} WHERE id = ?",
            (position_id,)
        ).fetchone()
        # position_lock instrumentation (2026-08-01), mirrors open_position()'s
        # -- proves the lock genuinely serializes a concurrent close attempt
        # against the same row, not just that the surrounding code runs.
        # Mode must reflect is_dry_run_sim like open_position() already does
        # (2026-08-01 Opus review finding) -- without it, a synthetic
        # dry-run-sim close (e.g. the FAZ canary) logs mode='live', letting a
        # purely synthetic event count toward the real-world readiness number.
        if row is None:
            # No is_dry_run_sim to key off for an already-gone row -- 'live'
            # here is a display default only, not a claim about mode, and
            # this branch is excluded from bad_results-based readiness
            # scoring at the registry level regardless (see coverage_registry.py).
            log_coverage_event("position_lock", 'live', position_id=position_id, result="already_closed",
                                detail="close_position")
            return False
        _mode = 'paper' if paper else ('dry_run' if row[4] else 'live')
        if exit_price is not None and row[0]:
            log_trade_exit(row[0], exit_signal_price, exit_price, exit_time, exit_reason, row[1], paper=paper,
                            exit_bar_time=exit_bar_time)
        c.execute(f"DELETE FROM {positions_table} WHERE id = ?", (position_id,))
        c.commit()
        # 2026-08-01 2nd Opus review finding: logged BEFORE log_trade_exit/DELETE
        # ran, so a raise in either would leave a real 'closed' event on record
        # for a close that never actually happened -- moved to after both succeed.
        log_coverage_event("position_lock", _mode, ticker=row[2], node_id=row[3], position_id=position_id,
                            result="closed", detail="close_position")
        return True


def log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares=None, paper=False,
                     is_dry_run_sim=False, signal_bar_time=None, position_source='core',
                     drought_confirm_days=None, drought_vol_gate=None, drought_gap_start=None,
                     drought_vol_pctile=None):
    _, trade_log_table = _pos_tables(paper)
    sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    signal_bar_time_str = (signal_bar_time.strftime('%Y-%m-%d %H:%M:%S')
                            if hasattr(signal_bar_time, 'strftime') else signal_bar_time)
    entry_drift    = (entry_price - signal_price) / signal_price * 100
    tp = node.get('take_profit')
    with _conn() as c:
        c.execute(f"""
            INSERT INTO {trade_log_table}
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct, arm_sell_pct, shares, account,
                 is_dry_run_sim, wl_id, signal_bar_time, position_source, drought_confirm_days,
                 drought_vol_gate, drought_gap_start, drought_vol_pctile)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, entry_drift, node.get('arm_sell_pct'),
            float(shares) if shares is not None else None, node.get('account'), 1 if is_dry_run_sim else 0,
            node.get('id'), signal_bar_time_str, position_source, drought_confirm_days,
            drought_vol_gate, drought_gap_start, drought_vol_pctile,
        ))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def log_trade_exit(trade_id, exit_signal_price, exit_price, exit_time, exit_reason, entry_price, paper=False,
                    exit_bar_time=None):
    _, trade_log_table = _pos_tables(paper)
    exit_time_str = exit_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(exit_time, 'strftime') else exit_time
    exit_bar_time_str = (exit_bar_time.strftime('%Y-%m-%d %H:%M:%S')
                          if hasattr(exit_bar_time, 'strftime') else exit_bar_time)
    exit_drift    = (exit_price - exit_signal_price) / exit_signal_price * 100
    pnl           = (exit_price - entry_price) / entry_price * 100
    with _conn() as c:
        c.execute(f"""
            UPDATE {trade_log_table} SET
                exit_signal_price = ?, exit_price = ?, exit_time = ?,
                exit_drift_pct = ?, pnl_pct = ?, exit_reason = ?, exit_bar_time = ?
            WHERE id = ?
        """, (float(exit_signal_price), float(exit_price), exit_time_str,
              exit_drift, pnl, exit_reason, exit_bar_time_str, trade_id))
        c.commit()


# ---------------------------------------------------------------------------
# Drought overlay, margin add-on-at-arm, skim-and-reserve -- 2026-08-09 build.
# See docs/design.md's 2026-08-07 "Live automation design" section and
# open_positions.position_source's schema comment above for the shape
# decisions (why drought reuses open_position()/open_positions, why add-on
# deliberately does not).
# ---------------------------------------------------------------------------

def open_drought_overlay_position(node, signal_price, signal_time, entry_price, entry_time,
                                   confirm_days, vol_gate=None, sl_pct=None, arm_pct=None, trail_pct=None,
                                   gap_start=None, vol_pctile=None, shares=None, paper=False,
                                   is_dry_run_sim=False, signal_bar_time=None):
    """Thin wrapper around open_position() -- a drought-overlay entry is
    real-shape-identical to a core entry (same ticker, same node's own SL/arm/
    trail params carried on the row by default), just tagged
    position_source='drought_overlay' plus the config (confirm_days/vol_gate)
    and observed-fact (gap_start/vol_pctile) snapshot columns.

    sl_pct/arm_pct/trail_pct mirror scripts/stacked_model/drought.py::
    generate_drought_trades' own param names and None-means-node's-own-default
    convention exactly, so live and backtest share one resolution order:
    explicit call-site value > the node's persisted drought_*_pct_override
    column > the node's own core fixed_sl/arm_sell_pct/trail_sell_pct. As of
    2026-08-09 nothing sets the override columns (SOXL's only validated signal
    uses plain node defaults, see drought_sl_pct_override's schema comment) --
    this resolution exists so a future re-validated override doesn't need
    another code change here, just a value in those columns or this call.
    Only safe to call when the node's core position is flat for this wl_id
    (drought is defined as filling the gap while core has no signal, and
    open_position()'s own dedup check keys on wl_id alone regardless of
    position_source -- a genuine second entry attempt while either is already
    open is correctly rejected as a duplicate, not routed around)."""
    overlay_node = dict(node)
    resolved_sl = sl_pct if sl_pct is not None else node.get('drought_sl_pct_override')
    resolved_arm = arm_pct if arm_pct is not None else node.get('drought_arm_pct_override')
    resolved_trail = trail_pct if trail_pct is not None else node.get('drought_trail_pct_override')
    if resolved_sl is not None:
        overlay_node['fixed_sl'] = resolved_sl
    if resolved_arm is not None:
        # Route to whichever column check_sell_condition's db._tp_or_arm_pct
        # actually reads for this node's strategy -- TrailingBothZScoreBreakout
        # stores its arm value in arm_sell_pct, every other strategy (including
        # TrailingExitZScoreBreakout) stores it in take_profit instead. Writing
        # unconditionally to arm_sell_pct would silently no-op the override for
        # a TrailingExit node -- the exact column-overload bug pattern already
        # found 3x elsewhere in this codebase (see take_profit/trail_buy_pct
        # notes in docs/backlog_cache.md), caught here before it became a 4th.
        if overlay_node['strategy'] == 'TrailingBothZScoreBreakout':
            overlay_node['arm_sell_pct'] = resolved_arm
        else:
            overlay_node['take_profit'] = resolved_arm
    if resolved_trail is not None:
        overlay_node['trail_sell_pct'] = resolved_trail
    return open_position(overlay_node, signal_price, signal_time, entry_price, entry_time, shares=shares,
                          paper=paper, is_dry_run_sim=is_dry_run_sim, signal_bar_time=signal_bar_time,
                          position_source='drought_overlay', drought_confirm_days=confirm_days,
                          drought_vol_gate=vol_gate, drought_gap_start=gap_start, drought_vol_pctile=vol_pctile)


def get_drought_overlay_position(wl_id, paper=False):
    """Explicit position_source filter, unlike get_open_position_by_wl_id --
    defensive: a caller asking specifically "is there an open drought
    position" should get None rather than a core row back if the two ever
    did somehow coexist (which would itself be a bug elsewhere, not something
    this getter should paper over)."""
    positions_table, _ = _pos_tables(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {positions_table} WHERE wl_id=? AND position_source='drought_overlay' "
            f"ORDER BY entry_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['trail_state'] = json.loads(d['trail_state']) if d.get('trail_state') else {}
    return d


def _addon_table(paper=False):
    return 'paper_addon_legs' if paper else 'addon_legs'


def open_addon_leg(parent_position, shares, entry_price, entry_time, paper=False, is_dry_run_sim=False,
                    entry_order_id=None, entry_status='filled'):
    """Opens a margin add-on-at-arm leg against an already-open CORE position
    (parent_position, e.g. from get_open_position_by_wl_id) -- deliberately its
    own table, not open_positions (see open_positions.position_source's schema
    comment for the collision reasoning). parent_trade_log_id is captured from
    parent_position['trade_log_id'] now, at creation time, since that's a
    permanent trade_log.id -- parent_position_id (open_positions.id) is only
    valid while the parent is still open and is not relied on after close.

    entry_order_id/entry_status are real-execution-only (paper always leaves
    them at the defaults, which write NULL/'filled' into columns paper_addon_legs
    doesn't even have -- fine, extra kwargs to a paper call site are simply
    unused by the INSERT below since it targets addon_legs' real columns only
    when paper=False; paper's own call site never passes them)."""
    if entry_status not in ('placed', 'filled', 'abandoned'):
        raise ValueError(f"invalid entry_status: {entry_status!r}")
    table = _addon_table(paper)
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    with _conn() as c:
        if paper:
            c.execute(f"""
                INSERT INTO {table}
                    (wl_id, parent_position_id, parent_trade_log_id, ticker, account, shares,
                     entry_price, entry_time, is_dry_run_sim)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                parent_position['wl_id'], parent_position['id'], parent_position['trade_log_id'],
                parent_position['ticker'], parent_position.get('account'), float(shares),
                float(entry_price), entry_time_str, 1 if is_dry_run_sim else 0,
            ))
        else:
            c.execute(f"""
                INSERT INTO {table}
                    (wl_id, parent_position_id, parent_trade_log_id, ticker, account, shares,
                     entry_price, entry_time, is_dry_run_sim, entry_order_id, entry_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                parent_position['wl_id'], parent_position['id'], parent_position['trade_log_id'],
                parent_position['ticker'], parent_position.get('account'), float(shares),
                float(entry_price), entry_time_str, 1 if is_dry_run_sim else 0,
                entry_order_id, entry_status,
            ))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def set_addon_leg_entry_filled(leg_id, entry_price, entry_status='filled', paper=False):
    """Confirms a real addon leg's market-buy fill -- entry_price is the real
    fill price (may differ from the placement-time estimate); entry_status
    moves 'placed' -> 'filled' (or 'abandoned' if check_addon_leg_reconciliation
    times it out unfilled)."""
    if entry_status not in ('filled', 'abandoned'):
        raise ValueError(f"invalid entry_status: {entry_status!r}")
    table = _addon_table(paper)
    with _conn() as c:
        c.execute(f"UPDATE {table} SET entry_price=?, entry_status=? WHERE id=?",
                   (float(entry_price), entry_status, leg_id))
        c.commit()


def set_addon_leg_exit_order_id(leg_id, order_id, paper=False):
    table = _addon_table(paper)
    with _conn() as c:
        c.execute(f"UPDATE {table} SET exit_order_id=? WHERE id=?", (order_id, leg_id))
        c.commit()


def set_addon_leg_sl_order_id(leg_id, order_id, broker_stop_price=None):
    """D3: records the leg's own real resting protective STOP order id/price
    (addon_legs only -- paper never places a real stop, no paper counterpart
    needed)."""
    with _conn() as c:
        c.execute("UPDATE addon_legs SET sl_order_id=?, broker_stop_price=? WHERE id=?",
                   (order_id, broker_stop_price, leg_id))
        c.commit()


def get_open_addon_leg_by_parent(parent_position_id, paper=False):
    table = _addon_table(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {table} WHERE parent_position_id=? AND status='open' "
            f"ORDER BY entry_time DESC LIMIT 1",
            (parent_position_id,)
        ).fetchone()
    return dict(row) if row else None


def get_open_addon_leg_by_wl_id(wl_id, paper=False):
    """wl_id-scoped counterpart to get_open_addon_leg_by_parent -- needed by
    check_addon_trigger_real/check_order's is_addon_leg preconditions, which
    have the node/wl_id in scope before the parent open_positions row's id is
    necessarily fresh in hand."""
    table = _addon_table(paper)
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM {table} WHERE wl_id=? AND status='open' ORDER BY entry_time DESC LIMIT 1",
            (wl_id,)
        ).fetchone()
    return dict(row) if row else None


def get_open_addon_legs(paper=False):
    table = _addon_table(paper)
    with _conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE status='open' ORDER BY entry_time").fetchall()]


# Mirrors scripts/stacked_model/add_on.py::MARGIN_COST_FLAT_PCT (validated
# negligible-but-real average margin-interest cost, ~33h typical hold --
# duplicated rather than imported for the same reason _DROUGHT_TARGET_H0/H1
# are duplicated in paper_trading.py: avoids pulling that module's backtester/
# numba transitive imports into signals_db.py, which the live daemon always
# loads. Found missing here by Opus review, 2026-08-09 -- close_addon_leg's
# pnl_pct was 0.04pp optimistic vs. the validated model on every leg.
_ADDON_MARGIN_COST_FLAT_PCT = 0.04


def close_addon_leg(leg_id, exit_price, exit_time, exit_reason, paper=False):
    """Closes an addon leg in isolation -- callers driving the real lockstep
    close (leg closes exactly when its parent core position closes, per the
    design's state machine) call this alongside close_position(), not instead
    of it; this function has no awareness of the parent's own close and does
    not enforce the lockstep itself."""
    table = _addon_table(paper)
    exit_time_str = exit_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(exit_time, 'strftime') else exit_time
    with _conn() as c:
        row = c.execute(f"SELECT entry_price FROM {table} WHERE id=?", (leg_id,)).fetchone()
        if row is None:
            return False
        pnl = (exit_price - row['entry_price']) / row['entry_price'] * 100 - _ADDON_MARGIN_COST_FLAT_PCT
        c.execute(f"""
            UPDATE {table} SET status='closed', exit_price=?, exit_time=?, exit_reason=?, pnl_pct=?
            WHERE id=?
        """, (float(exit_price), exit_time_str, exit_reason, pnl, leg_id))
        c.commit()
        return True


def get_skim_reserve_pool(account, reserve_ticker='SPY'):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM skim_reserve_pool WHERE account=? AND reserve_ticker=?",
            (account, reserve_ticker)
        ).fetchone()
    return dict(row) if row else {'account': account, 'reserve_ticker': reserve_ticker,
                                   'reserve_shares': 0.0, 'avg_cost': None}


def update_skim_reserve_pool(account, shares_delta, price, reserve_ticker='SPY'):
    """Upserts the ONE pooled reserve position per (account, reserve_ticker) --
    see skim_reserve_pool's schema comment for why this is pooled, not
    per-node. Buying more (shares_delta > 0) blends avg_cost by the standard
    weighted-average-cost convention; selling (shares_delta < 0, a redeploy)
    reduces shares without touching avg_cost (the remaining shares' cost basis
    is unchanged by a partial sale)."""
    with _conn() as c:
        row = c.execute(
            "SELECT reserve_shares, avg_cost FROM skim_reserve_pool WHERE account=? AND reserve_ticker=?",
            (account, reserve_ticker)
        ).fetchone()
        if row is None:
            old_shares, old_cost = 0.0, None
        else:
            old_shares, old_cost = row['reserve_shares'], row['avg_cost']
        new_shares = old_shares + shares_delta
        if shares_delta > 0:
            old_cost_basis = old_shares * old_cost if old_cost is not None else 0.0
            new_cost = (old_cost_basis + shares_delta * price) / new_shares if new_shares > 0 else price
        else:
            new_cost = old_cost
        c.execute("""
            INSERT INTO skim_reserve_pool (account, reserve_ticker, reserve_shares, avg_cost, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account, reserve_ticker) DO UPDATE SET
                reserve_shares=excluded.reserve_shares, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at
        """, (account, reserve_ticker, new_shares, new_cost))
        c.commit()


def record_skim_event(wl_id, account, action, amount, reserve_shares_delta=None, reserve_price=None,
                       reference_value=None, detail=None, new_balance_override=None):
    """Appends to skim_reserve_log (pure history) and updates the node's own
    skim_reserve_balance ledger by `amount` (positive for a skim moving money
    INTO the reserve, negative for a redeploy moving money OUT) -- these two
    writes are the ledger side; update_skim_reserve_pool (the physical
    reserve_ticker shares) is called separately by the caller, since a
    'redeploy_alert' event (no real order placed yet) updates neither the
    ledger balance nor the pool, only logs that the alert fired.

    new_balance_override: pass the caller's own already-computed balance
    (e.g. paper_trading.check_paper_skim, which marks the reserve to a real
    SPY price BEFORE applying this event and would otherwise disagree with
    this function's naive current_balance+amount, which knows nothing about
    that mark-to-market step) instead of deriving it from
    current_balance+amount here. None (default) preserves the original
    derive-it-yourself behavior for callers with no reason to override it."""
    with _conn() as c:
        if new_balance_override is not None:
            new_balance = new_balance_override
        else:
            row = c.execute("SELECT skim_reserve_balance FROM watch_list WHERE id=?", (wl_id,)).fetchone()
            current_balance = row['skim_reserve_balance'] if row else 0.0
            new_balance = current_balance + amount if action != 'redeploy_alert' else current_balance
        if action != 'redeploy_alert':
            c.execute("UPDATE watch_list SET skim_reserve_balance=? WHERE id=?", (new_balance, wl_id))
        c.execute("""
            INSERT INTO skim_reserve_log
                (wl_id, account, action, amount, reserve_shares_delta, reserve_price,
                 ledger_balance_after, reference_value, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (wl_id, account, action, float(amount), reserve_shares_delta, reserve_price,
              new_balance, reference_value, detail))
        c.commit()


_UNSET = object()


def set_skim_state(wl_id, skim_ref=_UNSET, skim_peak_before_decline=_UNSET, skim_min_since_peak=_UNSET,
                    skim_declining=_UNSET, skim_alert_sent_at=_UNSET, skim_alert_80_sent=_UNSET,
                    skim_alert_100_sent=_UNSET, skim_strategy_value=_UNSET, skim_last_mark_time=_UNSET,
                    skim_reserve_balance=_UNSET):
    """Persists the redeploy-trigger tracking state (mirrors
    scripts/stacked_model/skim_reserve.py's validated in-memory loop
    variables of the same names -- see that module's 2026-08-08 CRITICAL fix
    comment for why min_since_peak specifically must persist rather than be
    re-derived). Only fields actually PASSED are updated (default sentinel
    _UNSET, not None) -- callers typically update a subset per poll cycle, and
    unlike a plain None default, this lets skim_alert_sent_at=None be passed
    explicitly to clear the alert-sent marker back to NULL when a fresh
    decline cycle starts, without that clear being indistinguishable from
    "don't touch this field"."""
    fields, values = [], []
    for col, val in (('skim_ref', skim_ref), ('skim_peak_before_decline', skim_peak_before_decline),
                      ('skim_min_since_peak', skim_min_since_peak), ('skim_declining', skim_declining),
                      ('skim_alert_sent_at', skim_alert_sent_at), ('skim_alert_80_sent', skim_alert_80_sent),
                      ('skim_alert_100_sent', skim_alert_100_sent), ('skim_strategy_value', skim_strategy_value),
                      ('skim_last_mark_time', skim_last_mark_time), ('skim_reserve_balance', skim_reserve_balance)):
        if val is not _UNSET:
            fields.append(f"{col}=?")
            values.append(val)
    if not fields:
        return
    with _conn() as c:
        c.execute(f"UPDATE watch_list SET {', '.join(fields)} WHERE id=?", (*values, wl_id))
        c.commit()
