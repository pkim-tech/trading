"""
Tests for db_cache.active_builds / scripts.promote_derived_build -- the explicit
promotion pointer that replaced massive_hourly_derived/massive_minute_derived's
old `ORDER BY id DESC` "latest build" resolution (real 2026-08-26 incident: a
narrower rebuild silently became "latest" with zero completeness check, see
docs/deep_backlog.md's "active_builds promotion table" entry). Uses an isolated
sqlite file (never the real research DB) with a minimal massive_hourly_derived_
builds/massive_hourly_derived/massive_minute_derived schema, built via db_cache's
own writers so the fixture matches production shape.
"""
import sqlite3

import pandas as pd
import pytest

import db_cache
import scripts.promote_derived_build as promote_mod


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_cache, "DB_PATH", str(tmp_path / "test_universe.db"))
    return db_cache.DB_PATH


def _hourly_df(start, n, freq="h"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000},
        index=idx,
    )


def _make_build(ticker, label, df, row_count=None):
    """Writes one full build (metadata row + hourly rows) via db_cache's real
    writers, matching how build_massive_hourly_derived.py actually produces one."""
    build_id = db_cache.record_massive_hourly_build(
        ticker, label, raw_data_pulled_at="2026-08-01", raw_data_start=str(df.index.min()),
        raw_data_end=str(df.index.max()), dividend_data_asof="2026-08-01",
        row_count=row_count or len(df), correction_count=0)
    db_cache.write_massive_hourly_derived(ticker, build_id, df)
    return build_id


def test_active_build_resolves_over_newer_unpromoted_build(isolated_db):
    """Core fix: a genuinely newer/higher build_id must NOT silently become
    'active' just by existing -- get_massive_hourly_ohlcv must keep resolving
    the explicitly-promoted build until a human promotes something else."""
    df_v1 = _hourly_df("2021-01-01", 100)
    build1 = _make_build("TEST", "v1 full history", df_v1)
    db_cache.promote_active_build("TEST", "hourly", build1)

    # A newer, narrower build lands (e.g. a bad refresh) -- higher build_id,
    # but never promoted.
    df_v2_narrow = _hourly_df("2026-01-01", 10)
    _make_build("TEST", "v2 narrower (bad refresh)", df_v2_narrow)

    resolved = db_cache.get_massive_hourly_ohlcv("TEST")
    assert len(resolved) == 100, "resolution must stay on the promoted build, not silently jump to the newer one"
    assert resolved.index.min() == df_v1.index.min()


def test_promotion_switches_active_build(isolated_db):
    df_v1 = _hourly_df("2021-01-01", 100)
    build1 = _make_build("TEST", "v1", df_v1)
    db_cache.promote_active_build("TEST", "hourly", build1)

    df_v2 = _hourly_df("2021-01-01", 150)  # genuinely wider, not narrower
    build2 = _make_build("TEST", "v2 wider", df_v2)

    with sqlite3.connect(isolated_db) as conn:
        args = _Args(ticker="TEST", table="hourly", build_id=build2, force=False, note=None)
        rc = promote_mod.cmd_promote(conn, args)
    assert rc == 0
    assert db_cache.get_active_build_id("TEST", "hourly") == build2
    assert len(db_cache.get_massive_hourly_ohlcv("TEST")) == 150


def test_refuse_to_narrow_guard_blocks_narrower_promotion(isolated_db):
    """The real, non-eyeballed proof the refuse-to-narrow guard works: a narrower
    build_id must be REFUSED (nonzero exit, active build unchanged) without
    --force, and must succeed WITH --force."""
    df_wide = _hourly_df("2021-01-01", 200)
    build_wide = _make_build("TEST", "wide", df_wide)
    db_cache.promote_active_build("TEST", "hourly", build_wide)

    df_narrow = _hourly_df("2026-01-01", 10)  # much later start, way narrower
    build_narrow = _make_build("TEST", "narrower", df_narrow)

    with sqlite3.connect(isolated_db) as conn:
        args = _Args(ticker="TEST", table="hourly", build_id=build_narrow, force=False, note=None)
        rc = promote_mod.cmd_promote(conn, args)
    assert rc != 0, "a narrower promotion without --force must be refused (nonzero exit)"
    assert db_cache.get_active_build_id("TEST", "hourly") == build_wide, \
        "active build must NOT change when the promotion was refused"

    with sqlite3.connect(isolated_db) as conn:
        args = _Args(ticker="TEST", table="hourly", build_id=build_narrow, force=True, note=None)
        rc = promote_mod.cmd_promote(conn, args)
    assert rc == 0, "the same narrower promotion WITH --force must succeed"
    assert db_cache.get_active_build_id("TEST", "hourly") == build_narrow


def test_refuse_to_narrow_checks_both_ends(isolated_db):
    """Narrower on the END (regressing to an earlier last-bar) must also be
    refused, not just a later start date -- 'range', not just 'start'."""
    df_wide = _hourly_df("2021-01-01", 200)
    build_wide = _make_build("TEST", "wide", df_wide)
    db_cache.promote_active_build("TEST", "hourly", build_wide)

    # Same start, but ends earlier (fewer bars) -- narrower on the end only.
    df_short_end = _hourly_df("2021-01-01", 50)
    build_short_end = _make_build("TEST", "shorter tail", df_short_end)

    with sqlite3.connect(isolated_db) as conn:
        args = _Args(ticker="TEST", table="hourly", build_id=build_short_end, force=False, note=None)
        rc = promote_mod.cmd_promote(conn, args)
    assert rc != 0
    assert db_cache.get_active_build_id("TEST", "hourly") == build_wide


def test_promotion_refuses_empty_build(isolated_db):
    """An empty/orphan build_id (metadata row exists, zero price rows -- the
    exact orphan case get_massive_hourly_derived used to have to skip around)
    must be refused outright, never promoted."""
    df_wide = _hourly_df("2021-01-01", 100)
    build_wide = _make_build("TEST", "wide", df_wide)
    db_cache.promote_active_build("TEST", "hourly", build_wide)

    orphan_build_id = db_cache.record_massive_hourly_build(
        "TEST", "orphan (process died before write)", "2026-08-01", "2026-08-01",
        "2026-08-01", "2026-08-01", row_count=0, correction_count=0)

    with sqlite3.connect(isolated_db) as conn:
        args = _Args(ticker="TEST", table="hourly", build_id=orphan_build_id, force=True, note=None)
        rc = promote_mod.cmd_promote(conn, args)
    assert rc != 0, "an empty build must be refused even with --force"
    assert db_cache.get_active_build_id("TEST", "hourly") == build_wide


def test_migrate_backfills_old_resolution_as_a_no_op(isolated_db):
    """The migration must reproduce exactly what the OLD `ORDER BY id DESC`
    resolution would have returned -- a behavioral no-op for every ticker until
    a human explicitly promotes something different."""
    df_v1 = _hourly_df("2021-01-01", 100)
    build1 = _make_build("TEST", "v1", df_v1)
    df_v2 = _hourly_df("2021-01-01", 150)
    build2 = _make_build("TEST", "v2 newer, wider", df_v2)
    # No promotion yet -- active_builds is empty, exactly the pre-migration state.

    assert db_cache.get_active_build_id("TEST", "hourly") is None
    # Old-style resolution (what migration should reproduce): highest build_id with rows.
    with sqlite3.connect(isolated_db) as conn:
        old_style = promote_mod._old_style_latest_with_rows(conn, "TEST", "hourly")
    assert old_style == build2

    with sqlite3.connect(isolated_db) as conn:
        promote_mod.cmd_migrate(conn)

    assert db_cache.get_active_build_id("TEST", "hourly") == build2
    assert len(db_cache.get_massive_hourly_ohlcv("TEST")) == 150


def test_migrate_never_overwrites_an_existing_promotion(isolated_db):
    """Idempotency/safety: --migrate must never touch a (ticker, table_name) that
    already has a real active_builds row, even if a newer build_id exists."""
    df_v1 = _hourly_df("2021-01-01", 100)
    build1 = _make_build("TEST", "v1", df_v1)
    db_cache.promote_active_build("TEST", "hourly", build1)  # explicit promotion

    df_v2 = _hourly_df("2021-01-01", 150)
    _make_build("TEST", "v2 newer, wider, but NOT promoted", df_v2)

    with sqlite3.connect(isolated_db) as conn:
        promote_mod.cmd_migrate(conn)

    assert db_cache.get_active_build_id("TEST", "hourly") == build1, \
        "migrate must never override an already-promoted build"


def test_no_active_build_returns_empty_frame_not_an_error(isolated_db):
    """A ticker with real build rows but no promotion yet (brand-new build,
    or pre-migration) resolves the same way the old 'no build at all' case
    did -- empty DataFrame from get_massive_hourly_derived, which the _ohlcv
    wrapper then turns into a loud ValueError (unchanged, existing behavior)."""
    df = _hourly_df("2021-01-01", 100)
    _make_build("TEST", "built but never promoted", df)

    result = db_cache.get_massive_hourly_derived("TEST")
    assert result.empty

    with pytest.raises(ValueError, match="no massive_hourly_derived rows"):
        db_cache.get_massive_hourly_ohlcv("TEST")


class _Args:
    def __init__(self, ticker, table, build_id, force, note):
        self.ticker = ticker
        self.table = table
        self.build_id = build_id
        self.force = force
        self.note = note
