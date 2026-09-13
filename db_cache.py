import json
import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "cache" / "research" / "trading_universe.db")
# tickdata.db: massive_* tick-data tables (hourly/minute/second derived + builds +
# dividends_raw) plus active_builds (their promotion pointer) -- split out
# 2026-09-12, see docs/deep_backlog.md, since it's the bulk of trading_universe.db's
# size and has an entirely separate write pattern (bulk rebuild, not incremental
# trade/signal rows).
TICKDATA_DB_PATH = str(Path(__file__).parent / "cache" / "research" / "tickdata.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_cache (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)


def _ensure_mutation_log_table(conn):
    # data_mutation_log: traceability (not full immutability/versioning, per the
    # user's 2026-07-22 call -- see docs/research_log.md's 2026-07-22 entry) for
    # every split-guard rescale of a cached *_1h.csv file. The rescale itself is
    # scale-invariant to the %-based signals every strategy trades on, so re-running
    # the same code against today's cache should reproduce a past backtest_cache
    # number without needing the exact old bytes -- this table exists so the *fact*
    # that a rescale happened (when/why/how) is never silently lost, and the actual
    # pre-rescale data is still recoverable via pre_mutation_snapshot if ever needed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_mutation_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT NOT NULL,
            factor               REAL NOT NULL,
            detected_at          TEXT NOT NULL DEFAULT (datetime('now')),
            overlap_bar_time     TEXT,
            price_before         REAL,
            price_after          REAL,
            notes                TEXT,
            pre_mutation_snapshot TEXT
        )
    """)


def log_data_mutation(ticker, factor, overlap_bar_time, price_before, price_after,
                       notes, pre_mutation_df):
    """Records one split-guard rescale event, with the full pre-rescale
    DataFrame (CSV-serialized) so the actual old data is recoverable, not
    just the fact that it changed. Called only from data_manager.py's rescale
    branch, right before it overwrites df_local in place."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_mutation_log_table(conn)
        conn.execute("""
            INSERT INTO data_mutation_log
                (ticker, factor, overlap_bar_time, price_before, price_after, notes, pre_mutation_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticker, factor, overlap_bar_time, price_before, price_after, notes,
              pre_mutation_df.to_csv()))


def get_data_mutations(ticker=None, limit=200):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_mutation_log_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT id, ticker, factor, detected_at, overlap_bar_time, price_before, price_after, notes FROM data_mutation_log"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id DESC LIMIT ?"
        params = params + (limit,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _ensure_bad_tick_scan_table(conn):
    # bad_tick_scan_log: one row per ticker per scan_bad_ticks.py run, recording
    # the actual date range/row count scanned -- not just the hits found. Built
    # 2026-08-11 after the DFEN bad-tick investigation: without recording what
    # window was actually scanned, a rerun with a shifted cache window (new bars
    # appended, or a ticker's data refetched) produces different results with no
    # way to tell "the data changed" from "the scan logic changed" from "a new
    # bad tick appeared." hits_json is the full hit list for that ticker (usually
    # empty), so a scan's findings are permanent even if the underlying CSV is
    # later patched.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bad_tick_scan_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL DEFAULT (datetime('now')),
            ticker       TEXT NOT NULL,
            date_start   TEXT,
            date_end     TEXT,
            row_count    INTEGER,
            threshold    REAL,
            recovery_frac REAL,
            hits_count   INTEGER,
            hits_json    TEXT
        )
    """)


def log_bad_tick_scan(ticker, date_start, date_end, row_count, threshold,
                       recovery_frac, hits):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_bad_tick_scan_table(conn)
        conn.execute("""
            INSERT INTO bad_tick_scan_log
                (ticker, date_start, date_end, row_count, threshold, recovery_frac,
                 hits_count, hits_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, date_start, date_end, row_count, threshold, recovery_frac,
              len(hits), json.dumps(hits, default=str)))


def get_bad_tick_scans(ticker=None, limit=500):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_bad_tick_scan_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM bad_tick_scan_log"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id DESC LIMIT ?"
        params = params + (limit,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _ensure_sweep_tranches_table(conn):
    # sweep_tranches: DB-backed replacement for scripts/liquidity_tranches.txt's
    # hand-edited ticker-membership list, same rationale as the accounts table
    # replacing hardcoded account config (2026-08-11) and SCHWAB_AUTOMATION_TICKERS
    # moving off a Python literal -- a flat file that gets hand-edited repeatedly
    # (disqualified-ticker removals, priority reshuffles) has no audit trail of
    # who/when/why. This table is the source of truth; scripts/
    # render_liquidity_tranches.py regenerates the .txt file from it in the exact
    # format run_liquidity_tranches.sh already parses, so that script's tested
    # bash parsing logic doesn't need to change at all. Soft-delete (active=0),
    # never a hard DELETE, per standing convention -- a disqualification is itself
    # a real fact worth keeping, not just an absence.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_tranches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign     TEXT NOT NULL DEFAULT 'liquidity_screen',
            tranche_num  INTEGER NOT NULL,
            ticker       TEXT NOT NULL,
            active       INTEGER NOT NULL DEFAULT 1,
            added_at     TEXT NOT NULL DEFAULT (datetime('now')),
            removed_at   TEXT,
            reason       TEXT,
            UNIQUE(campaign, tranche_num, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_campaign_config (
            campaign     TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            fixed_sls    TEXT NOT NULL,
            strategies   TEXT NOT NULL,
            entry_timing TEXT NOT NULL,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def set_sweep_campaign_config(campaign, version, fixed_sls, strategies, entry_timing):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            INSERT INTO sweep_campaign_config (campaign, version, fixed_sls, strategies, entry_timing, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(campaign) DO UPDATE SET
                version=excluded.version, fixed_sls=excluded.fixed_sls,
                strategies=excluded.strategies, entry_timing=excluded.entry_timing,
                updated_at=excluded.updated_at
        """, (campaign, version, fixed_sls, strategies, entry_timing))


def get_sweep_campaign_config(campaign):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sweep_campaign_config WHERE campaign=?", (campaign,)).fetchone()
        return dict(row) if row else None


def add_tranche_ticker(campaign, tranche_num, ticker, reason=None):
    """Adds a ticker to a tranche, or reactivates it (clears removed_at) if it
    was previously soft-removed under the same (campaign, tranche_num, ticker)
    -- a ticker moving back into scope (e.g. a disqualification reversed) keeps
    its original added_at rather than looking like a brand-new row."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            INSERT INTO sweep_tranches (campaign, tranche_num, ticker, active, reason)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(campaign, tranche_num, ticker) DO UPDATE SET
                active=1, removed_at=NULL, reason=excluded.reason
        """, (campaign, tranche_num, ticker, reason))


def remove_tranche_ticker(campaign, tranche_num, ticker, reason):
    """Soft-removes a ticker from a tranche -- reason is required (not optional)
    since a removal with no recorded why is exactly the discrepancy-tracking gap
    this table exists to close."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            UPDATE sweep_tranches SET active=0, removed_at=datetime('now'), reason=?
            WHERE campaign=? AND tranche_num=? AND ticker=?
        """, (reason, campaign, tranche_num, ticker))


def get_tranches(campaign='liquidity_screen', active_only=True):
    """Returns {tranche_num: [ticker, ...]}, tickers in insertion (added_at) order
    within each tranche."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT tranche_num, ticker FROM sweep_tranches"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY tranche_num, added_at, id"
        rows = conn.execute(q).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['tranche_num'], []).append(r['ticker'])
    return out


def get_tranche_audit(campaign='liquidity_screen'):
    """Full history including inactive (removed) rows, for review -- every
    removal carries its reason, never silently dropped."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM sweep_tranches WHERE campaign=?
            ORDER BY tranche_num, added_at, id
        """, (campaign,)).fetchall()
    return [dict(r) for r in rows]


def get_kv(key):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT value FROM kv_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_kv(key, value):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache(key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, json.dumps(value))
        )


def refresh_dropdown_cache():
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)

        versions = [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()]
        set_kv("versions", versions)

        for v in versions:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM backtest_cache WHERE version = ? ORDER BY ticker", (v,)
            ).fetchall()]
            strategies = [r[0] for r in conn.execute(
                "SELECT DISTINCT strategy FROM backtest_cache WHERE version = ? ORDER BY strategy", (v,)
            ).fetchall()]
            set_kv(f"tickers_{v}", tickers)
            set_kv(f"strategies_{v}", strategies)

            # strats_by_ticker for Spatial Topology
            strats_by_ticker = {}
            for t in tickers:
                strats_by_ticker[t] = strategies  # same strategies available for all tickers
            set_kv(f"strats_by_ticker_{v}", strats_by_ticker)

    print(f"Cached {len(versions)} versions: {versions}")


def refresh_pivot_cache(versions=None):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        if versions is None:
            versions = [r[0] for r in conn.execute(
                "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
            ).fetchall()]

        for v in versions:
            print(f"  pivot cache: {v}...")

            df_cells = pd.read_sql_query("""
                SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                       trades, MAX(strategy_return) AS strategy_return
                FROM backtest_cache
                WHERE version = ? AND window IN (10, 20, 30)
                GROUP BY ticker, window, z_score_threshold, trades
            """, conn, params=(v,))

            # Best node per ticker for alpha/bh metadata
            df_meta = pd.read_sql_query("""
                WITH best AS (
                    SELECT ticker, strategy_return, alpha_vs_spy, asset_bh,
                           CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh ELSE NULL END AS bh_mult,
                           ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY strategy_return DESC) AS rn
                    FROM backtest_cache
                    WHERE version = ? AND window IN (10, 20, 30)
                )
                SELECT ticker, alpha_vs_spy, asset_bh, bh_mult FROM best WHERE rn = 1
            """, conn, params=(v,))

            set_kv(f"pivot_cells_{v}", df_cells.to_dict(orient="records"))
            set_kv(f"pivot_meta_{v}", df_meta.to_dict(orient="records"))

    print(f"Pivot cache refreshed for {len(versions)} versions")


# Qualified 3x index tickers, holds collapsed to best alpha per (tp, sl) node.
# Shared by the Top Pivot cliff-safe section (live fallback) and the sweep-end refresh.
CLIFF_GRID_SQL = """
    SELECT b.ticker, b.strategy, b.version, b.window,
           COALESCE(b.z_score_threshold, 2.0) AS z,
           b.axis_tp AS take_profit, b.stop_loss,
           MAX(b.alpha_vs_spy) AS max_alpha,
           MAX(b.asset_bh)     AS bh
    FROM backtest_cache b
    JOIN (
        SELECT symbol FROM tickers
        WHERE leverage = 3
          AND (inverse IS NULL OR inverse = 0)
          AND index_underlier IS NOT NULL AND index_underlier != ''
          AND (dupe_direxion IS NULL OR dupe_direxion = '')
          AND avg_vol_10d IS NOT NULL AND last_price IS NOT NULL
          AND avg_vol_10d * last_price >= 5000000
    ) q ON q.symbol = b.ticker
    WHERE b.trades >= ?
    GROUP BY b.ticker, b.strategy, b.version, b.window,
             COALESCE(b.z_score_threshold, 2.0), b.axis_tp, b.stop_loss
"""


def load_cliff_grid(min_trades=5):
    """kv-cached at sweep completion; falls back to the heavy live query (~2 min)."""
    cached = get_kv(f"cliff_grid_mt{min_trades}")
    if cached is not None:
        return pd.DataFrame(cached)
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(CLIFF_GRID_SQL, conn, params=(min_trades,))


def refresh_cliff_grid_cache(min_trades=5):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        df = pd.read_sql_query(CLIFF_GRID_SQL, conn, params=(min_trades,))
    set_kv(f"cliff_grid_mt{min_trades}", df.to_dict(orient="records"))
    print(f"Cliff grid cache refreshed ({len(df):,} nodes, min_trades={min_trades})")


def refresh_best_nodes_cache():
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        versions = [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()]

        for v in versions:
            print(f"  best_nodes cache: {v}...")
            rows = conn.execute("""
                WITH best AS (
                    SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                           axis_tp, stop_loss, max_hold_hours,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker, window, COALESCE(z_score_threshold, 2.0)
                               ORDER BY alpha_vs_spy DESC
                           ) AS rn
                    FROM backtest_cache WHERE version = ?
                )
                SELECT ticker, window, z, axis_tp, stop_loss, max_hold_hours
                FROM best WHERE rn = 1
            """, (v,)).fetchall()
            data = {f"{r[0]}|{int(r[1])}|{float(r[2])}": [int(r[3]), int(r[4]), int(r[5])] for r in rows}
            set_kv(f"best_nodes_{v}", data)

    print(f"Best nodes cache refreshed for {len(versions)} versions")


def _ensure_massive_dividends_table(conn):
    # massive_dividends_raw: cached copy of Massive.com's /stocks/v1/dividends
    # results, per ticker -- raw, untouched, never mutated. Avoids re-fetching
    # (and re-burning rate-limit budget) every time build_massive_hourly_derived.py
    # runs. Only historical_adjustment_factor is needed downstream (per Massive's
    # documented rule: for a bar on date D, find the first dividend whose
    # ex_dividend_date is after D and multiply price by that dividend's factor --
    # cumulative, so only the nearest future one is ever applied).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_dividends_raw (
            ticker                        TEXT NOT NULL,
            ex_dividend_date              TEXT NOT NULL,
            historical_adjustment_factor  REAL NOT NULL,
            cash_amount                   REAL,
            fetched_at                    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, ex_dividend_date)
        )
    """)


def cache_massive_dividends(ticker, records):
    """records: list of dicts from the Massive dividends API response (raw
    'results' array). Upserts by (ticker, ex_dividend_date) -- a rerun for the
    same ticker just refreshes factors rather than duplicating rows."""
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_dividends_table(conn)
        for r in records:
            conn.execute("""
                INSERT INTO massive_dividends_raw
                    (ticker, ex_dividend_date, historical_adjustment_factor, cash_amount, fetched_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(ticker, ex_dividend_date) DO UPDATE SET
                    historical_adjustment_factor=excluded.historical_adjustment_factor,
                    cash_amount=excluded.cash_amount, fetched_at=excluded.fetched_at
            """, (ticker, r["ex_dividend_date"], r["historical_adjustment_factor"], r.get("cash_amount")))


def get_massive_dividends(ticker):
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_dividends_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ex_dividend_date, historical_adjustment_factor FROM massive_dividends_raw "
            "WHERE ticker=? ORDER BY ex_dividend_date", (ticker,)
        ).fetchall()
        return [dict(r) for r in rows]


def _migrate_massive_hourly_tables_to_build_id(conn):
    """One-time migration (2026-08-22): massive_hourly_derived/massive_hourly_
    corrections originally had no build_id column (single-vintage, overwrite-in-
    place design). Renames the old tables aside rather than dropping them -- they
    hold pre-fix data from the killed 19-ticker batch (SOXL/KORU are the only
    ones already rebuilt under the corrected pipeline; the other 17 are still the
    known-buggy pre-fix rows) -- preserved for inspection, not needed for
    reconstruction (that only needs the untouched raw minute/dividend caches)."""
    for table in ("massive_hourly_derived", "massive_hourly_corrections"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and "build_id" not in cols:
            old_name = f"{table}_pre_build_id_migration"
            # No DROP here (found by paired review 2026-08-22): if this migration
            # ever re-triggered for any reason, a DROP would destroy the exact
            # preserved data it exists to protect. Not currently reachable (the
            # guard above requires a build_id-less table, and the new schema
            # always has one), but the old drop-then-rename was one accidental
            # re-run away from being destructive. Suffix-increment instead.
            n = 1
            target = old_name
            existing = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (f"{old_name}%",)).fetchall()}
            while target in existing:
                n += 1
                target = f"{old_name}_{n}"
            conn.execute(f"ALTER TABLE {table} RENAME TO {target}")


def _ensure_massive_hourly_derived_table(conn):
    _migrate_massive_hourly_tables_to_build_id(conn)
    # massive_hourly_derived: the DERIVED hourly series built from Massive minute
    # data (dividend+split adjusted, resampled) -- this is what the GT/hourly
    # kernels should read for a ticker's full history, per the 2026-08-22 pipeline
    # decision (Massive is the sole source across the full range; Yahoo hourly
    # stays separate, audit-only, never consumed). `build_id` (added 2026-08-22,
    # references massive_hourly_derived_builds.id) tags every row with which
    # rebuild produced it -- every vintage's full price series is kept
    # permanently side by side, never overwritten in place (measured cost: ~64MB
    # for one full 88-ticker vintage, ~650MB for 10 accumulated vintages -- trivial
    # against this DB's existing multi-GB size, so no reason to throw old vintages
    # away). Read the CURRENT vintage via get_massive_hourly_derived() (joins to
    # the latest build_id per ticker); pass an explicit build_id to read an older
    # one for reproducing what a past backtest campaign actually saw.
    # `corrected`=1 marks a bar whose Open/High/Low/Close was adjusted by the
    # spike-correction step -- see massive_hourly_corrections for the full detail.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_hourly_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            volume     REAL,
            corrected  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_hourly_corrections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            build_id    INTEGER NOT NULL,
            ts          TEXT NOT NULL,
            field       TEXT NOT NULL,
            raw_value   REAL NOT NULL,
            new_value   REAL NOT NULL,
            reason      TEXT NOT NULL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # massive_hourly_derived_builds: the vintage/provenance record for each per-
    # ticker rebuild -- two independent freshness axes (2026-08-22 design), since
    # they change independently: a new dividend can trigger a re-adjustment with NO
    # new raw data pulled, and new raw minute bars can be pulled with no new
    # dividend. raw_data_pulled_at is a best-effort proxy (the {ticker}_1m.csv file's
    # own mtime) for existing files that predate this tracking -- going forward,
    # any script that pulls NEW raw minute data should record a real pull timestamp
    # instead of relying on mtime, which isn't a reliable provenance signal.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_hourly_derived_builds (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """)


def _connect_or_reuse(conn):
    """Returns (connection, owns_it). When conn is None (every existing standalone
    caller, unchanged), opens+returns a fresh connection the caller must use as its
    own context manager (owns_it=True). When conn is provided (2026-08-22, added to
    let build_ticker() wrap build-row + hourly + corrections + minute writes in ONE
    atomic transaction -- paired review found the previous one-connection-per-write
    design could leave the hourly leg on a newer build_id than the minute leg if the
    process died mid-build, silently pairing two different dividend/raw-data
    vintages with no error), the caller manages commit/rollback itself."""
    if conn is not None:
        return conn, False
    return sqlite3.connect(TICKDATA_DB_PATH), True


def write_massive_hourly_derived(ticker, build_id, df, conn=None):
    """df: DataFrame indexed by tz-naive hourly timestamp, columns
    Open/High/Low/Close/Volume/corrected (corrected optional, defaults 0). Inserts
    a fresh, permanent row set under this build_id -- never deletes or overwrites
    a prior build_id's rows (every vintage kept side by side, see table docstring
    above). build_id must come from record_massive_hourly_build()'s return value.
    Pass conn to participate in a caller-managed transaction (see _connect_or_reuse);
    default (conn=None) is the original standalone-connection behavior, unchanged."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_hourly_derived_table(c)
        rows = [
            (ticker, build_id, ts.strftime("%Y-%m-%d %H:%M:%S"), float(r["Open"]), float(r["High"]),
             float(r["Low"]), float(r["Close"]), float(r.get("Volume", 0) or 0),
             int(r.get("corrected", 0)))
            for ts, r in df.iterrows()
        ]
        c.executemany("""
            INSERT INTO massive_hourly_derived (ticker, build_id, ts, open, high, low, close, volume, corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def log_massive_hourly_correction(ticker, build_id, ts, field, raw_value, new_value, reason, conn=None):
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_hourly_derived_table(c)
        c.execute("""
            INSERT INTO massive_hourly_corrections (ticker, build_id, ts, field, raw_value, new_value, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticker, build_id, ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else ts,
              field, float(raw_value), float(new_value), reason))
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def record_massive_hourly_build(ticker, label, raw_data_pulled_at, raw_data_start,
                                 raw_data_end, dividend_data_asof, row_count, correction_count,
                                 conn=None):
    """One row per rebuild -- the vintage/provenance record. Every rebuild is its
    own permanent history entry (matches data_mutation_log's append-only
    philosophy) and its own build_id, which write_massive_hourly_derived() and
    log_massive_hourly_correction() tag their rows with -- every vintage's full
    price series and corrections are kept side by side, never overwritten.
    Returns the new build_id."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_hourly_derived_table(c)
        cur = c.execute("""
            INSERT INTO massive_hourly_derived_builds
                (ticker, label, raw_data_pulled_at, raw_data_start, raw_data_end,
                 dividend_data_asof, row_count, correction_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, label, raw_data_pulled_at, raw_data_start, raw_data_end,
              dividend_data_asof, row_count, correction_count))
        build_id = cur.lastrowid
        if owns:
            c.commit()
        return build_id
    finally:
        if owns:
            c.close()


def _migrate_active_builds_table_widen_check(conn):
    """One-time migration (2026-08-29): active_builds' table_name CHECK constraint
    was hardcoded to ('hourly', 'minute') -- adding 'second' (the new dividend-
    adjusted 1-second derived pipeline, see massive_second_derived/
    massive_second_derived_builds -- its OWN independent build_id sequence, not
    shared with hourly/minute) requires a real schema migration since SQLite has
    no ALTER TABLE ... CHECK. Idempotent: checks sqlite_master's stored CREATE TABLE
    SQL for the widened CHECK before doing anything, so re-running this (or calling
    it twice in the same process) is a no-op the second time. The RENAME/CREATE/
    INSERT/verify/DROP sequence below runs inside the caller's own transaction --
    every caller reaches this via `_connect_or_reuse` against TICKDATA_DB_PATH (commits
    only if the whole block succeeds, rolls back on any exception), so a failure at
    any point (including the explicit row-count check) leaves the original table
    completely untouched under its original name."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='active_builds'"
    ).fetchone()
    if row is None:
        return  # fresh DB -- _ensure_active_builds_table's own CREATE below already has the widened CHECK
    existing_sql = row[0] or ""
    if "'second'" in existing_sql:
        return  # already migrated

    old_count = conn.execute("SELECT COUNT(*) FROM active_builds").fetchone()[0]
    conn.execute("ALTER TABLE active_builds RENAME TO active_builds_pre_second_migration")
    conn.execute("""
        CREATE TABLE active_builds (
            ticker      TEXT NOT NULL,
            table_name  TEXT NOT NULL CHECK (table_name IN ('hourly', 'minute', 'second')),
            build_id    INTEGER NOT NULL,
            promoted_at TEXT NOT NULL DEFAULT (datetime('now')),
            note        TEXT,
            PRIMARY KEY (ticker, table_name)
        )
    """)
    conn.execute("""
        INSERT INTO active_builds (ticker, table_name, build_id, promoted_at, note)
        SELECT ticker, table_name, build_id, promoted_at, note FROM active_builds_pre_second_migration
    """)
    new_count = conn.execute("SELECT COUNT(*) FROM active_builds").fetchone()[0]
    if new_count != old_count:
        raise RuntimeError(
            f"active_builds migration row-count mismatch: old={old_count} new={new_count} -- "
            f"aborting (transaction will roll back, old table preserved as "
            f"active_builds_pre_second_migration under its original name)."
        )
    conn.execute("DROP TABLE active_builds_pre_second_migration")


def _ensure_active_builds_table(conn):
    _migrate_active_builds_table_widen_check(conn)
    # active_builds: explicit promotion pointer for massive_hourly_derived_builds --
    # get_massive_hourly_derived/get_massive_minute_derived's no-build_id path resolves
    # from here instead of `ORDER BY b.id DESC` (real incident, 2026-08-26: a
    # fetch_massive_minute_data.py refresh with no --years flag silently truncated
    # SOXL/DPST/DFEN's canonical minute archive, and the resulting narrower build
    # silently became "latest" purely by insertion order, with zero completeness/
    # freshness check -- see docs/deep_backlog.md's 2026-08-26 "active_builds
    # promotion table" entry for the full incident). A build only becomes active via
    # an explicit scripts/promote_derived_build.py call, never automatically on
    # creation or on write_massive_hourly_derived()/write_massive_minute_derived().
    #
    # Compound key (ticker, table_name) rather than one row per ticker -- confirmed
    # directly against the real DB (2026-08-28, before this table existed) that
    # hourly's and minute's own "latest build WITH ROWS" resolution CAN diverge for
    # the same ticker (a build that died mid-write between the two legs would leave
    # hourly on a newer build_id than minute, or vice versa -- see
    # write_massive_hourly_derived's docstring). Zero real cases of divergence found
    # across all 82 tickers at migration time, but the schema doesn't assume it can't
    # happen, so hourly and minute are promoted (and can be re-promoted) independently.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_builds (
            ticker      TEXT NOT NULL,
            table_name  TEXT NOT NULL CHECK (table_name IN ('hourly', 'minute', 'second')),
            build_id    INTEGER NOT NULL,
            promoted_at TEXT NOT NULL DEFAULT (datetime('now')),
            note        TEXT,
            PRIMARY KEY (ticker, table_name)
        )
    """)


# (ticker, table_name) pairs already logged this process -- see
# _log_build_id_resolution's docstring. Real gap found 2026-08-28: get_massive_
# hourly_ohlcv/get_massive_minute_ohlcv correctly resolve through active_builds
# (verified directly: SOXL -> build_id=174, byte-identical to an explicit
# request), but nothing ever LOGGED which build_id got used for a given run --
# if a build is ever superseded later, there's no record of which vintage an
# old sweep actually saw. Logging only (no backtest_cache.build_id column --
# that's separate, bigger scope, not part of this fix).
_logged_build_id_resolutions = set()


def _log_build_id_resolution(ticker, table_name, build_id):
    """Prints once per (ticker, table_name) per PROCESS -- not once per call.
    A sweep's ProcessPoolExecutor workers are separate OS processes (forked
    after run_optimization_sweep.run()'s sys.stdout Tee is already installed,
    so each worker's inherited sys.stdout still reaches the real run log
    file), each with its own copy of this module-level set -- this correctly
    logs the FIRST resolution per (ticker, table_name) in each worker that
    touches it, rather than flooding output on every one of the many calls a
    single sweep phase makes for the same ticker."""
    key = (ticker, table_name)
    if key in _logged_build_id_resolutions:
        return
    _logged_build_id_resolutions.add(key)
    print(f"[db_cache] {ticker} massive_{table_name}_derived resolved active build_id={build_id}")


def get_active_build_id(ticker, table_name, conn=None):
    """The explicit-promotion resolution used by get_massive_hourly_derived/
    get_massive_minute_derived's no-build_id path. Returns None if no build has ever
    been promoted for this ticker/table_name (a brand-new build that hasn't been
    promoted yet, or a ticker that predates scripts/promote_derived_build.py --migrate
    having been run) -- callers treat that the same as "no build at all" (existing
    loud-failure convention), never silently falling back to insertion order."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_active_builds_table(c)
        row = c.execute(
            "SELECT build_id FROM active_builds WHERE ticker=? AND table_name=?",
            (ticker, table_name)).fetchone()
        return row[0] if row else None
    finally:
        if owns:
            c.close()


def promote_active_build(ticker, table_name, build_id, note=None, conn=None):
    """The ONLY way active_builds changes -- see scripts/promote_derived_build.py for
    the safety-checked CLI wrapping this (refuse-to-narrow guard without --force,
    migration backfill mode, before/after logging). Idempotent re-promotion of the
    same (ticker, table_name) just updates build_id/promoted_at/note in place --
    active_builds is a pointer, not an append-only history (massive_hourly_derived_
    builds is the permanent provenance record; this table only ever tracks the
    CURRENT pointer)."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_active_builds_table(c)
        c.execute("""
            INSERT INTO active_builds (ticker, table_name, build_id, promoted_at, note)
            VALUES (?, ?, ?, datetime('now'), ?)
            ON CONFLICT(ticker, table_name) DO UPDATE SET
                build_id=excluded.build_id, promoted_at=excluded.promoted_at, note=excluded.note
        """, (ticker, table_name, build_id, note))
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def get_latest_massive_hourly_build(ticker):
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_hourly_derived_table(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM massive_hourly_derived_builds WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        return dict(row) if row else None


def get_massive_hourly_derived(ticker, build_id=None):
    """Returns the ACTIVE (explicitly promoted) vintage by default -- see
    get_active_build_id/active_builds table. Pass an explicit build_id (from
    massive_hourly_derived_builds) to reproduce what an older vintage looked like
    -- e.g. to exactly recreate the inputs a past backtest campaign actually saw.

    Resolution used to be a naive `ORDER BY id DESC` (latest build_id THAT ACTUALLY
    HAS ROWS, to skip an orphan build_id -- record_massive_hourly_build()/
    write_massive_hourly_derived() are two separate, uncoordinated writes, and a
    process dying in between used to leave an orphan build_id with zero matching
    rows, see the 2026-08-22 paired review this orphan-skip was built for). That
    insertion-order-only resolution is what silently let a narrower rebuild become
    "latest" with no completeness/freshness check (real 2026-08-26 incident, see
    active_builds table's own docstring) -- replaced with an explicit promotion
    pointer instead. No orphan-skip logic needed here anymore: promotion only ever
    points at a build scripts/promote_derived_build.py already confirmed has rows."""
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_hourly_derived_table(conn)
        import pandas as pd
        if build_id is None:
            build_id = get_active_build_id(ticker, 'hourly', conn=conn)
            if build_id is None:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "corrected"])
            _log_build_id_resolution(ticker, 'hourly', build_id)
        df = pd.read_sql_query(
            "SELECT ts, open AS Open, high AS High, low AS Low, close AS Close, "
            "volume AS Volume, corrected FROM massive_hourly_derived WHERE ticker=? AND build_id=? ORDER BY ts",
            conn, params=(ticker, build_id), parse_dates=["ts"])
        return df.set_index("ts")


def _ensure_massive_second_derived_table(conn):
    # massive_second_derived: the dividend-adjusted 1-SECOND derived series, built
    # from cache/research/second_data/{ticker}_1s.csv raw ticks (2026-08-29,
    # scripts/build_massive_second_derived.py). Dividend adjustment for this leg
    # happens mostly at MASSIVE'S OWN source (their seconds-aggregates endpoint's
    # adjusted=true bakes in dividends, unlike the minute/hourly aggregates
    # endpoint's split-only adjusted=true) -- the builder script applies its own
    # apply_dividend_adjustment only as a residual top-up for any real dividend
    # after the raw pull date, NOT a full independent adjustment pass the way the
    # hourly/minute legs do (2026-09-06 fix, see that script's own docstring for
    # the double-adjustment bug this closed). Same column shape as
    # massive_hourly_derived (including `corrected`, kept for shape consistency
    # with the sibling tables even though it's always 0 here: there is no spike-
    # correction step for seconds, no Yahoo-second reference exists to cross-check
    # against). PRIMARY KEY (ticker, build_id, ts), same never-overwritten multi-
    # vintage convention as the hourly/minute tables.
    #
    # build_id is its OWN independent AUTOINCREMENT sequence (massive_second_
    # derived_builds below), NOT shared with massive_hourly_derived_builds/
    # massive_minute_derived_builds -- deliberate design choice: the hourly and
    # minute legs share one build_id because build_massive_hourly_derived.py
    # produces both from the exact same Massive-minute-API pull in one pass (so
    # "same build" is a real, meaningful concept there). The raw 1-second data is a
    # wholly separate source (pre-fetched CSVs on disk, not derived from that same
    # minute-API pull), so there is no equivalent "same build" relationship to
    # preserve -- giving it its own id sequence avoids implying a provenance link
    # that doesn't exist.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_second_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            volume     REAL,
            corrected  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """)
    # massive_second_derived_builds: the vintage/provenance record for each per-
    # ticker second-leg rebuild -- same columns as massive_hourly_derived_builds
    # (built_at, raw_data_start/end, dividend_data_asof, row_count,
    # correction_count -- correction_count is always 0, see table docstring above).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_second_derived_builds (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """)


def write_massive_second_derived(ticker, build_id, df, conn=None):
    """df: DataFrame indexed by tz-naive second timestamp, columns Open/High/Low/
    Close/Volume (dividend-adjusted -- mostly at Massive's own source, plus a
    residual top-up for any post-pull dividend; see build_massive_second_derived.py's
    docstring), 'corrected' optional (defaults 0 -- always 0 in practice, no
    spike-correction step for seconds). Same permanent, never-
    overwritten-in-place, multi-vintage convention as write_massive_hourly_derived
    -- build_id must come from record_massive_second_build()'s return value. Builds
    row tuples via zip() over numpy/pandas arrays rather than df.iterrows()
    (mirrors write_massive_minute_derived's own reasoning -- iterrows() boxes every
    row into a Series, too slow over the multi-million-row second-level history a
    full ticker build has). Pass conn to participate in a caller-managed transaction
    (see _connect_or_reuse); default (conn=None) opens/commits/closes its own
    connection."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_second_derived_table(c)
        ts_str = df.index.strftime("%Y-%m-%d %H:%M:%S")
        volume = df["Volume"].fillna(0).astype(float) if "Volume" in df.columns else [0.0] * len(df)
        corrected = df["corrected"].astype(int) if "corrected" in df.columns else [0] * len(df)
        rows = list(zip([ticker] * len(df), [build_id] * len(df), ts_str,
                         df["Open"].astype(float), df["High"].astype(float),
                         df["Low"].astype(float), df["Close"].astype(float),
                         volume, corrected))
        c.executemany("""
            INSERT INTO massive_second_derived (ticker, build_id, ts, open, high, low, close, volume, corrected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def record_massive_second_build(ticker, label, raw_data_pulled_at, raw_data_start,
                                 raw_data_end, dividend_data_asof, row_count, correction_count,
                                 conn=None):
    """Second leg's provenance-record function -- mirrors record_massive_hourly_
    build() exactly, except this mints its OWN independent build_id (AUTOINCREMENT
    on massive_second_derived_builds), not shared with the hourly/minute builds
    tables (see massive_second_derived's table docstring for why). Returns the new
    build_id."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_second_derived_table(c)
        cur = c.execute("""
            INSERT INTO massive_second_derived_builds
                (ticker, label, raw_data_pulled_at, raw_data_start, raw_data_end,
                 dividend_data_asof, row_count, correction_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, label, raw_data_pulled_at, raw_data_start, raw_data_end,
              dividend_data_asof, row_count, correction_count))
        build_id = cur.lastrowid
        if owns:
            c.commit()
        return build_id
    finally:
        if owns:
            c.close()


def get_massive_second_derived(ticker, build_id=None):
    """Returns the ACTIVE (explicitly promoted) vintage by default -- see
    get_massive_hourly_derived's docstring for the full active_builds-based
    resolution rationale (table_name='second', promoted independently from
    hourly/minute). Pass an explicit build_id to reproduce an older vintage.

    Chunked read (2026-09-06, paired-review MEDIUM/HIGH finding -- real OOM risk under
    worker-pool contention): a single non-chunked pd.read_sql_query() over a SOXL-scale
    table (~22M rows) measured ~10.8GB peak RSS for one process -- sqlite3's DBAPI
    materializes the whole result set as Python row tuples before pandas ever builds
    columnar arrays, several times the ~1GB the resulting DataFrame itself occupies.
    Under a real 8-worker ProcessPoolExecutor (each worker a separate process, no shared
    memory), even 2 concurrent large-ticker loads would exceed this box's 15GB RAM.
    Reads via chunksize instead (same pattern as scripts/build_massive_second_derived.py's
    2026-09-06 CSV-chunking fix) so peak memory is bounded by one chunk's raw row-tuple
    overhead plus the running concatenated total, not the full raw result set at once."""
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_second_derived_table(conn)
        if build_id is None:
            build_id = get_active_build_id(ticker, 'second', conn=conn)
            if build_id is None:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "corrected"])
            _log_build_id_resolution(ticker, 'second', build_id)
        parts = []
        for chunk in pd.read_sql_query(
            "SELECT ts, open AS Open, high AS High, low AS Low, close AS Close, "
            "volume AS Volume, corrected FROM massive_second_derived WHERE ticker=? AND build_id=? ORDER BY ts",
            conn, params=(ticker, build_id), parse_dates=["ts"], chunksize=500_000,
        ):
            parts.append(chunk.set_index("ts"))
        if not parts:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "corrected"])
        return pd.concat(parts, copy=False)


def get_massive_second_ohlcv(ticker, build_id=None):
    """Drop-in-style accessor mirroring get_massive_hourly_ohlcv/get_massive_
    minute_ohlcv exactly (same Open/High/Low/Close/Volume columns, tz-naive
    DatetimeIndex, sorted ascending, Volume cast to int64) for the dividend-
    adjusted 1-second derived series. No consumer is wired to use this yet
    (2026-08-29) -- exists as the accessor, matching the sibling resolution/
    logging conventions (_log_build_id_resolution etc.). Raises if the ticker has
    no second build at all, matching the sibling loud-failure convention."""
    df = get_massive_second_derived(ticker, build_id=build_id)
    if df.empty:
        raise ValueError(
            f"get_massive_second_ohlcv: no massive_second_derived rows for ticker={ticker!r} "
            f"(build_id={build_id!r}) -- run scripts/build_massive_second_derived.py first."
        )
    df = df.drop(columns=["corrected"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    df.index.name = "timestamp"
    return df


def _ensure_massive_minute_derived_table(conn):
    # massive_minute_derived: the dividend-adjusted MINUTE series, the sibling of
    # massive_hourly_derived (2026-08-22 fix -- build_ticker() computed the adjusted
    # minute dataframe all along but only ever persisted its hourly resample; the
    # GT kernel's intrabar SL/TP/TRAIL fill-price checks were left reading raw,
    # UNADJUSTED minute CSVs regardless of --data-source, a real ~1.6%+ inconsistency
    # vs. the adjusted hourly leg that grows further back in time). Shares
    # massive_hourly_derived_builds' build_id/provenance record rather than a
    # separate builds table -- one build_ticker() run produces both artifacts from
    # the exact same dividend/raw-data vintage in one pass, so they're never out of
    # sync. Regular-session-only (09:30-16:00 ET), matching every real consumer's own
    # filter (sim_minute_groundtruth_independent.load_minutes,
    # run_optimization_sweep._load_minute_df) -- no reason to persist the extended-
    # hours rows nothing downstream reads. No 'corrected' column: the spike-correction
    # step only ever operates on the hourly aggregate, never the source minute bars --
    # KNOWN RESIDUAL GAP (flagged by paired review 2026-08-22, both contextual-Opus and
    # Fable-cold): a bar the hourly leg neutralizes as a fabricated spike still has its
    # ORIGINAL (uncorrected) prices in this table, so a GT kernel intrabar SL/TP/TRAIL
    # check reading the massive-source minute leg could still fire on the same bad tick
    # the hourly leg declared fake. Not fixed here -- correcting the same event at
    # minute resolution needs its own detection pass (the round-trip/wick detectors in
    # build_massive_hourly_derived.py operate on hourly aggregates, not raw minutes) and
    # is out of scope for the dividend-adjustment fix this table exists for. In practice
    # bounded: SOXL's full build had 10 corrections across 8,755 hourly bars (~0.1%).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_minute_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """)


def write_massive_minute_derived(ticker, build_id, df, conn=None):
    """df: DataFrame indexed by tz-naive minute timestamp, columns Open/High/Low/
    Close (dividend-adjusted). Same permanent, never-overwritten-in-place, multi-
    vintage convention as write_massive_hourly_derived -- build_id must come from
    record_massive_hourly_build()'s return value (shared with the hourly leg of
    the same build). Pass conn to participate in a caller-managed transaction (see
    _connect_or_reuse); default (conn=None) opens/commits/closes its own connection.

    Builds the row tuples via zip() over numpy arrays rather than df.iterrows()
    (found slow -- iterrows() boxes every row into a Series -- over the ~500k-1M
    rows a full minute history has; zip over raw arrays avoids that per-row cost)."""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_minute_derived_table(c)
        ts_str = df.index.strftime("%Y-%m-%d %H:%M:%S")
        rows = list(zip([ticker] * len(df), [build_id] * len(df), ts_str,
                         df["Open"].astype(float), df["High"].astype(float),
                         df["Low"].astype(float), df["Close"].astype(float)))
        c.executemany("""
            INSERT INTO massive_minute_derived (ticker, build_id, ts, open, high, low, close)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def get_massive_minute_derived(ticker, build_id=None):
    """Returns the ACTIVE (explicitly promoted) vintage by default -- see
    get_massive_hourly_derived's docstring for the full rationale (same
    active_builds-based resolution, promoted independently per table_name='minute'
    since hourly's and minute's own "latest build with rows" CAN diverge -- see
    active_builds table's own docstring). Pass an explicit build_id to reproduce an
    older vintage exactly."""
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_minute_derived_table(conn)
        if build_id is None:
            build_id = get_active_build_id(ticker, 'minute', conn=conn)
            if build_id is None:
                return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
            _log_build_id_resolution(ticker, 'minute', build_id)
        df = pd.read_sql_query(
            "SELECT ts, open AS Open, high AS High, low AS Low, close AS Close "
            "FROM massive_minute_derived WHERE ticker=? AND build_id=? ORDER BY ts",
            conn, params=(ticker, build_id), parse_dates=["ts"])
        return df.set_index("ts")


def get_massive_minute_ohlcv(ticker, build_id=None):
    """Drop-in replacement for sim_minute_groundtruth_independent.load_minutes() /
    run_optimization_sweep._load_minute_df()'s raw-CSV read (same Open/High/Low/
    Close columns, same tz-naive DatetimeIndex, sorted ascending) -- except
    dividend-adjusted, unlike the raw minute CSV. Raises if the ticker has no
    minute build at all, matching get_massive_hourly_ohlcv's loud-failure
    convention (a silent empty frame here would look identical to "no minutes
    traded" to every downstream intrabar check)."""
    df = get_massive_minute_derived(ticker, build_id=build_id)
    if df.empty:
        raise ValueError(
            f"get_massive_minute_ohlcv: no massive_minute_derived rows for ticker={ticker!r} "
            f"(build_id={build_id!r}) -- run scripts/build_massive_hourly_derived.py first."
        )
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df.index.name = "timestamp"
    return df


def _ensure_massive_minute_derived_builds_table(conn):
    # massive_minute_derived_builds: minute's provenance sibling to
    # massive_hourly_derived_builds (2026-08-29 design, docs/design.md's 2026-08-29
    # (very late) entry) -- SAME shape, same columns, deliberately not redesigned.
    # `id` is NOT autoincrement here: build_ticker() writes both legs of a build
    # from the exact same dividend/raw-data vintage in one pass, sharing ONE
    # build_id (record_massive_hourly_build()'s return value) between
    # massive_hourly_derived and massive_minute_derived -- this table's `id`
    # mirrors that same value explicitly (record_massive_minute_build() inserts
    # it, never lets SQLite generate its own), so massive_minute_derived_builds.id
    # always equals massive_hourly_derived_builds.id for the same real build event
    # and the two tables stay trivially joinable. correction_count is always 0 for
    # every minute build (real, not unknown/placeholder): the spike-correction step
    # only ever operates on the hourly aggregate, never the raw minute bars -- see
    # massive_minute_derived's own table docstring for the known residual gap this
    # implies (a bad tick the hourly leg corrects can still be present, uncorrected,
    # in the minute leg).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS massive_minute_derived_builds (
            id                    INTEGER PRIMARY KEY,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """)


def record_massive_minute_build(build_id, ticker, label, raw_data_pulled_at, raw_data_start,
                                 raw_data_end, dividend_data_asof, row_count, correction_count=0,
                                 built_at=None, conn=None):
    """Minute's provenance-record sibling to record_massive_hourly_build() -- but
    takes build_id as an explicit argument rather than generating/returning one:
    the real build_id always comes from record_massive_hourly_build()'s own return
    value (called first, same build_ticker() pass, same transaction), never
    independently minted here. Upserts (INSERT OR REPLACE) rather than a bare
    INSERT so this is safe to call twice for the same build_id (e.g. a backfill
    script re-run) without violating the PRIMARY KEY.

    built_at: pass the REAL value explicitly when known (e.g. a backfill reusing
    a sibling massive_hourly_derived_builds row's own built_at -- the true past
    build time, not now). Omit (None, the default) for a genuine fresh build
    happening right now -- build_ticker()'s own real-time call relies on this
    default. Precedence, in order: explicit built_at argument > this row's own
    already-stored built_at (so a re-run of the SAME call never clobbers a
    previously-recorded value with 'now') > datetime('now') as the final
    fallback for a genuinely first-ever INSERT with no built_at supplied.
    (Fixed 2026-08-29, contextual Opus review, CONFIRMED HIGH: an earlier
    version had no built_at parameter at all, so the backfill script's call
    silently stamped every backfilled row with the BACKFILL's own run time
    instead of the real historical build time it had already looked up from
    the sibling hourly row -- contradicted that script's own docstring and
    corrupted exactly the incident-forensics trail this table exists for.)"""
    c, owns = _connect_or_reuse(conn)
    try:
        _ensure_massive_minute_derived_builds_table(c)
        c.execute("""
            INSERT OR REPLACE INTO massive_minute_derived_builds
                (id, ticker, label, built_at, raw_data_pulled_at, raw_data_start, raw_data_end,
                 dividend_data_asof, row_count, correction_count)
            VALUES (?, ?, ?, COALESCE(?, (SELECT built_at FROM massive_minute_derived_builds WHERE id=?),
                                       datetime('now')), ?, ?, ?, ?, ?, ?)
        """, (build_id, ticker, label, built_at, build_id, raw_data_pulled_at, raw_data_start, raw_data_end,
              dividend_data_asof, row_count, correction_count))
        if owns:
            c.commit()
    finally:
        if owns:
            c.close()


def get_massive_minute_derived_builds(ticker=None):
    with sqlite3.connect(TICKDATA_DB_PATH) as conn:
        _ensure_massive_minute_derived_builds_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM massive_minute_derived_builds"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _ensure_derived_build_adjustments_table(conn):
    # derived_build_adjustments: a 1-to-many, append-only event log recording real
    # detected/applied corporate-action events against the Massive-derived
    # hourly/minute pipeline -- distinct from massive_hourly_derived_builds/
    # massive_minute_derived_builds (which describe "what does build X look like",
    # not "what real-world event happened and when"). Reuses data_mutation_log's
    # PATTERN (ticker, detection fields, notes, append-only), not the table itself
    # -- data_mutation_log predates active_builds (no build_id concept), is
    # split-only, and is scoped to the legacy yahoo _1h.csv split-guard rescale
    # path specifically, a different pathway from this one. See docs/design.md's
    # 2026-08-29 (very late) entry for the full design rationale.
    #
    # Column choices, deviating slightly from the design doc's own sketch:
    # - event_type has no CHECK constraint (unlike the doc's ['dividend'/'split'/
    #   'other'] suggestion) -- deliberately open string, matching this project's
    #   existing convention of not over-constraining a freeform-ish classification
    #   column this early (e.g. coverage_events.scenario_key is also unconstrained
    #   TEXT). A CHECK can be added later once real usage confirms the real value
    #   set; loosening a CHECK is a schema migration, widening a convention isn't.
    # - magnitude and cash_amount are BOTH present (not a single "magnitude/ratio"
    #   column as sketched) -- mirrors massive_dividends_raw's own two-field split
    #   (historical_adjustment_factor vs cash_amount), since a real dividend event
    #   naturally carries both a ratio-style adjustment factor AND a raw cash
    #   amount, and a split event only ever has the former. Collapsing them into
    #   one ambiguous column would lose real information for no benefit.
    # - resulting_build_id is nullable INTEGER with no FK enforcement (SQLite FKs
    #   aren't enabled project-wide here) -- a detected event can predate any
    #   rebuild it eventually triggers, exactly as the design doc specifies.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS derived_build_adjustments (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker             TEXT NOT NULL,
            event_type         TEXT NOT NULL,
            detected_at        TEXT NOT NULL DEFAULT (datetime('now')),
            event_date         TEXT,
            magnitude          REAL,
            cash_amount        REAL,
            source             TEXT NOT NULL,
            resulting_build_id INTEGER,
            notes              TEXT
        )
    """)


def log_derived_build_adjustment(ticker, event_type, event_date, magnitude, cash_amount,
                                  source, resulting_build_id, notes):
    """Records one detected/applied corp-action event against the Massive-derived
    pipeline. NOT wired into anything live yet (2026-08-29) -- no code currently
    detects-and-triggers a rebuild automatically; that's the separate, bigger,
    not-yet-built rolling-window/canary-triggered-rebuild mechanism from
    docs/design.md's 2026-08-29 entry. This is schema + a real insert/query helper
    pair only, matching log_data_mutation/get_data_mutations' own shape."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_derived_build_adjustments_table(conn)
        conn.execute("""
            INSERT INTO derived_build_adjustments
                (ticker, event_type, event_date, magnitude, cash_amount, source, resulting_build_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, event_type, event_date, magnitude, cash_amount, source, resulting_build_id, notes))


def get_derived_build_adjustments(ticker=None, limit=200):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_derived_build_adjustments_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM derived_build_adjustments"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id DESC LIMIT ?"
        params = params + (limit,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_massive_hourly_ohlcv(ticker, build_id=None):
    """Drop-in replacement for `pd.read_csv(f"{ticker}_1h.csv", index_col=0,
    parse_dates=True)` (the Yahoo-sourced hourly CSV every GT/hourly caller currently
    loads) -- same column set (Open/High/Low/Close/Volume), same tz-naive DatetimeIndex,
    sorted ascending, Volume cast to int64 to match the CSV's dtype exactly. Built
    2026-08-22 to wire massive_hourly_derived (dividend+split-adjusted, back to
    2021-08-23 for most tickers vs. Yahoo hourly's ~2023-07-24 floor) in as an
    opt-in alternate data source -- see --data-source flags in
    scripts/sim_minute_groundtruth_independent.py and the GT dispatch scripts.

    Drops the 'corrected' provenance column (not part of the CSV shape) and any
    all-NaN Volume rows' NaN (fills 0, matching write_massive_hourly_derived's own
    NULL->0 coercion) before the cast. Raises if the ticker has no build at all --
    a silent empty-frame return here would look identical to "no trades happened"
    several callers downstream, which is worse than a loud failure at load time."""
    df = get_massive_hourly_derived(ticker, build_id=build_id)
    if df.empty:
        raise ValueError(
            f"get_massive_hourly_ohlcv: no massive_hourly_derived rows for ticker={ticker!r} "
            f"(build_id={build_id!r}) -- run scripts/build_massive_hourly_derived.py first."
        )
    df = df.drop(columns=["corrected"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    df.index.name = "Datetime"
    return df


GT_WORKERS_CAP_ROW_THRESHOLD = 15_000_000
GT_WORKERS_CAPPED_VALUE = 4


def resolve_effective_gt_workers(massive_tickers, requested_workers, conn=None):
    """Row-count-keyed effective-workers cap for a ProcessPoolExecutor that resimulates
    real GT candidates at second resolution (2026-09-11, scripts/calibrate_gt_workers.py
    dispatch -- originally built for candidate_summary_report.run_gt_mode's outer
    per-scope pool, moved here 2026-09-11 so bench_phase1_phase2_inmemory.py's
    Phase2.5-cliffbox second-resolution pool -- the same underlying growth risk, a
    different call site -- can share it instead of duplicating the threshold/logic).

    Real mechanism: per-worker cache/state growth (_SECOND_DF_CACHE etc.) ACROSS a
    long-lived worker's many sequential candidates, not a single call's peak memory --
    confirmed via calibrate_gt_workers.py's --sustained mode, which showed per-worker
    PSS growth over 15 candidates scaling with ticker row count (AGQ +0.24GB/worker,
    TNA +0.47GB/worker) and SOXL at workers=8 genuinely exceeding safe memory under
    that realistic sustained load (a real, non-instant trip -- distinct from a
    single-cell test, which showed no ceiling at all for any of the three tickers).
    Keyed on real active-build row count (not a hardcoded ticker name) so any other
    ticker that grows into SOXL's row-count range later is covered automatically, and
    so AGQ/TNA-scale tickers aren't penalized by a blanket low default. Threshold
    picked from the actual measured growth rates: TNA (11.4M rows) stayed safe under
    sustained load, SOXL (22.2M) did not -- GT_WORKERS_CAP_ROW_THRESHOLD splits the
    two clusters.

    `massive_tickers`: real tickers in this run whose scopes use data_source='massive'
    (only those load second-resolution data at all -- a yahoo-sourced scope never hits
    this growth path). Below the threshold, `requested_workers` is returned as-is;
    above it, capped at GT_WORKERS_CAPPED_VALUE regardless of what was requested.
    `conn`: optional caller-managed connection; default (None) opens its own against
    this module's DB_PATH with a real timeout=60.0 (NOT _connect_or_reuse's plain
    sqlite3.connect(), which has only sqlite3's 5s default busy timeout) -- this call
    sits on bench_phase1_phase2_inmemory.py's Phase2.5 dispatch critical path, a real
    multi-hour run, so a lock/IO error here must not propagate and kill it (same
    fail-soft posture as campaign_registry.get_workers_budget, added 2026-08-31 after
    exactly that failure mode: an unhandled DB exception killed a bench process,
    discarding hours of completed in-memory work -- paired-review finding, both
    reviewers, 2026-09-11)."""
    if not massive_tickers:
        return requested_workers
    owns = conn is None
    try:
        c = conn if conn is not None else sqlite3.connect(TICKDATA_DB_PATH, timeout=60.0)
        try:
            placeholders = ",".join("?" for _ in massive_tickers)
            row_counts = dict(c.execute(f"""
                SELECT m.ticker, COUNT(*) FROM massive_second_derived m
                JOIN active_builds ab ON ab.ticker = m.ticker AND ab.table_name = 'second'
                                      AND ab.build_id = m.build_id
                WHERE m.ticker IN ({placeholders})
                GROUP BY m.ticker
            """, list(massive_tickers)).fetchall())
        finally:
            if owns:
                c.close()
    except sqlite3.Error as e:
        print(f"[workers cap] row-count lookup failed ({e!r}) -- not capping, "
              f"using requested --workers={requested_workers} as-is (fail-soft, same "
              f"posture as campaign_registry.get_workers_budget).")
        return requested_workers
    if not row_counts:
        return requested_workers
    worst_ticker = max(row_counts, key=row_counts.get)
    worst_rows = row_counts[worst_ticker]
    if worst_rows > GT_WORKERS_CAP_ROW_THRESHOLD and requested_workers > GT_WORKERS_CAPPED_VALUE:
        print(f"[workers cap] {worst_ticker}: capped --workers {requested_workers} -> "
              f"{GT_WORKERS_CAPPED_VALUE} -- active massive_second_derived row count "
              f"{worst_rows:,} exceeds the sustained-load-safe threshold "
              f"({GT_WORKERS_CAP_ROW_THRESHOLD:,}); per-worker cache/state growth across "
              f"a long multi-candidate run scales with ticker row count (see "
              f"scripts/calibrate_gt_workers.py --sustained), not just a single call's "
              f"peak memory.")
        return GT_WORKERS_CAPPED_VALUE
    return requested_workers


if __name__ == "__main__":
    refresh_dropdown_cache()
    refresh_pivot_cache()
    refresh_best_nodes_cache()
