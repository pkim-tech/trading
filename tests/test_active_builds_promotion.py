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
    # massive_*/active_builds now live in TICKDATA_DB_PATH, not DB_PATH (2026-09-12
    # tickdata.db split) -- this fixture's tables are all in that family, so it's
    # TICKDATA_DB_PATH that needs isolating.
    monkeypatch.setattr(db_cache, "TICKDATA_DB_PATH", str(tmp_path / "test_tickdata.db"))
    return db_cache.TICKDATA_DB_PATH


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


def test_build_id_resolution_is_logged_once_per_process(isolated_db, capsys, monkeypatch):
    """Real gap found 2026-08-28: the active_builds resolution path had zero
    logging of which build_id got used for a given run -- if a build is ever
    superseded, there's no record of which vintage an old sweep saw. Proves
    the log line fires on the first resolution and does NOT re-fire on a
    second call in the same process (the sweep's own dedupe-per-process ask,
    not a full silence -- a fresh process, e.g. a different worker, gets its
    own first log line, unaffected by this test's isolated set)."""
    monkeypatch.setattr(db_cache, "_logged_build_id_resolutions", set())
    df = _hourly_df("2021-01-01", 100)
    build_id = _make_build("TEST", "v1", df)
    db_cache.promote_active_build("TEST", "hourly", build_id)

    db_cache.get_massive_hourly_derived("TEST")
    out = capsys.readouterr().out
    assert f"TEST massive_hourly_derived resolved active build_id={build_id}" in out

    db_cache.get_massive_hourly_derived("TEST")
    out2 = capsys.readouterr().out
    assert out2 == "", f"must not re-log on a second call in the same process: {out2!r}"


def test_build_id_resolution_not_logged_for_explicit_build_id(isolated_db, capsys, monkeypatch):
    """Passing an explicit build_id (reproducing an older vintage on purpose)
    bypasses active_builds resolution entirely -- nothing to log, since
    there's no ambiguity about which build got used."""
    monkeypatch.setattr(db_cache, "_logged_build_id_resolutions", set())
    df = _hourly_df("2021-01-01", 100)
    build_id = _make_build("TEST", "v1", df)

    db_cache.get_massive_hourly_derived("TEST", build_id=build_id)
    out = capsys.readouterr().out
    assert out == "", f"an explicit build_id must not trigger resolution logging: {out!r}"


def _second_df(start, n, freq="s"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": 10.0, "High": 10.1, "Low": 9.9, "Close": 10.05, "Volume": 100},
        index=idx,
    )


def _make_second_build(ticker, label, df):
    """Second leg's equivalent of _make_build -- its OWN independent build_id
    sequence (massive_second_derived_builds), not shared with hourly/minute."""
    build_id = db_cache.record_massive_second_build(
        ticker, label, raw_data_pulled_at="2026-08-29", raw_data_start=str(df.index.min()),
        raw_data_end=str(df.index.max()), dividend_data_asof="2026-08-29",
        row_count=len(df), correction_count=0)
    db_cache.write_massive_second_derived(ticker, build_id, df)
    return build_id


def test_second_table_promotion_and_resolution(isolated_db):
    """The 2026-08-29 'second' pipeline: active_builds' CHECK now admits 'second',
    promotion works via the same path, and get_massive_second_ohlcv resolves
    through active_builds like its hourly/minute siblings."""
    df = _second_df("2021-08-25 09:30:00", 300)
    build_id = _make_second_build("TEST", "v1", df)

    # not promoted yet -> empty frame / loud ValueError, same as siblings
    assert db_cache.get_massive_second_derived("TEST").empty
    with pytest.raises(ValueError, match="no massive_second_derived rows"):
        db_cache.get_massive_second_ohlcv("TEST")

    db_cache.promote_active_build("TEST", "second", build_id)
    assert db_cache.get_active_build_id("TEST", "second") == build_id
    resolved = db_cache.get_massive_second_ohlcv("TEST")
    assert len(resolved) == 300
    assert list(resolved.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_second_has_independent_build_id_sequence(isolated_db):
    """massive_second_derived_builds mints its OWN ids -- an hourly build and a
    second build for the same ticker do NOT collide or share a build_id."""
    hourly_bid = _make_build("TEST", "h1", _hourly_df("2021-01-01", 50))
    second_bid = _make_second_build("TEST", "s1", _second_df("2021-08-25 09:30:00", 50))
    # both are id=1 in their OWN table -- proves the sequences are independent,
    # not a shared counter
    assert hourly_bid == 1
    assert second_bid == 1
    db_cache.promote_active_build("TEST", "hourly", hourly_bid)
    db_cache.promote_active_build("TEST", "second", second_bid)
    assert db_cache.get_active_build_id("TEST", "hourly") == hourly_bid
    assert db_cache.get_active_build_id("TEST", "second") == second_bid


def test_promote_second_via_cmd_promote_default_build_id(isolated_db):
    """cmd_promote's default (no --build-id) lookup for 'second' must query
    massive_second_derived_builds, NOT massive_hourly_derived_builds (which the
    hourly/minute path uses -- 'second' has its own sequence)."""
    b1 = _make_second_build("TEST", "s1", _second_df("2021-08-25 09:30:00", 100))
    b2 = _make_second_build("TEST", "s2", _second_df("2021-08-25 09:30:00", 200))  # newer, wider
    with sqlite3.connect(isolated_db) as conn:
        rc = promote_mod.cmd_promote(conn, _Args(ticker="TEST", table="second",
                                                 build_id=None, force=False, note=None))
    assert rc == 0
    assert db_cache.get_active_build_id("TEST", "second") == b2
    assert len(db_cache.get_massive_second_ohlcv("TEST")) == 200


class _Args:
    def __init__(self, ticker, table, build_id, force, note):
        self.ticker = ticker
        self.table = table
        self.build_id = build_id
        self.force = force
        self.note = note
