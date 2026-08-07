"""Clones live-track/daily-track node pairs for staged paper-testing of the
drought overlay / margin add-on / skim-and-reserve mechanisms, isolated from
the real v5 research nodes those mechanisms were validated against.

Rationale (2026-08-1x staged-testing session): flipping these flags directly
on SOXL's id=92/163 or AGQ's id=86/157 would be cheap, but those nodes are the
only source of real v5 paper-trading track record for those tickers -- and
the overlay code has already been shown to touch shared core-scan state (the
HANDOFF-before-core-scan fix touches _scan_pinned_entry, the same real entry
path every node flows through). A latent bug on first real activation could
corrupt data already relied on for decisions. Cloning is free in paper mode,
so isolate instead.

Two-tier design, per the user's explicit call: SOXL is the exhaustive
regression board (all 7 non-trivial combinations of drought/addon/skim --
2**3 - 1, "all off" being the real, already-running v5 node, no clone
needed), reusing its validated drought config regardless of whether addon's
real margin-eligibility would ever actually apply to SOXL (irrelevant in
paper -- no real orders exist for any of these mechanisms yet). AGQ carries
only the two REALISTIC candidate configs it could plausibly actually run
live (addon alone, already staged; addon+skim, new here) -- not the full
matrix, since AGQ isn't a drought candidate (rejected, see docs/research_log.md).

Live-track/daily-track pairing is included for every combo EXCEPT skim-alone:
skim's own logic never makes a price-timing-sensitive decision (it reacts to
an already-closed trade's recorded P&L; the only live-price read is a pure
mark-to-market lookup, not a signal check), so daily-track's Close-vs-tick
comparison adds no diagnostic value for that combo specifically. Every combo
that also includes drought or addon keeps the pairing, since those DO have
their own price-timing sensitivity independent of skim being present.

Clones use a distinct `version` tag per combo (not 'v5' or a bare
'v5-overlay-test' reused across combos on the same ticker) specifically so
add_node's dedup key (which includes version, and does NOT include any of
the drought/addon/skim override columns -- those are set via a follow-up
UPDATE, invisible to the dedup check) doesn't silently no-op two structurally
different combos against each other.

Usage:
    .venv/bin/python scripts/stage_overlay_test_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db

DROUGHT_CFG = {"drought_overlay_enabled": 1, "drought_confirm_days": 3, "drought_vol_gate": 0.4}
ADDON_CFG = {"addon_enabled": 1}
SKIM_CFG = {"skim_enabled": 1}


def _merge(*cfgs):
    out = {}
    for c in cfgs:
        out.update(c)
    return out


# (ticker, live_track_source_id, daily_track_source_id_or_None, version_tag, overlay_config)
# daily_track_source_id=None -> live-track-only combo (skim-alone).
COMBOS = [
    # SOXL -- exhaustive regression board, all 7 non-trivial combos.
    ("SOXL", 92, 163, "v5-overlay-test", DROUGHT_CFG),                                   # drought only (already staged)
    ("SOXL", 92, 163, "v5-overlay-test-a", ADDON_CFG),                                   # addon only
    ("SOXL", 92, 163, "v5-overlay-test-da", _merge(DROUGHT_CFG, ADDON_CFG)),              # drought+addon
    ("SOXL", 92, None, "v5-overlay-test-skim", SKIM_CFG),                                # skim only (live-track only)
    ("SOXL", 92, 163, "v5-overlay-test-skim-d", _merge(SKIM_CFG, DROUGHT_CFG)),           # skim+drought
    ("SOXL", 92, 163, "v5-overlay-test-skim-a", _merge(SKIM_CFG, ADDON_CFG)),             # skim+addon
    ("SOXL", 92, 163, "v5-overlay-test-skim-da", _merge(SKIM_CFG, DROUGHT_CFG, ADDON_CFG)),  # skim+drought+addon
    # AGQ -- realistic candidate configs only, not exhaustive.
    ("AGQ", 86, 157, "v5-overlay-test", ADDON_CFG),                                      # addon, no skim (already staged)
    ("AGQ", 86, 157, "v5-overlay-test-skim-a", _merge(SKIM_CFG, ADDON_CFG)),              # addon + skim
    # AGQ drought/drought+addon, added 2026-08-1x: a real, pre-existing gap --
    # every SOXL combo above is TrailingBothZScoreBreakout (trailing-buy
    # entry); AGQ is the only ticker in this matrix running the OTHER real
    # entry mechanism (TrailingExitZScoreBreakout, market-buy), and its
    # drought/drought+addon combos were never added, so the market-buy
    # drought-entry dispatch branch (notify_drought_buy_signal's
    # _attempt_automated_market_buy path) had zero paper-trading coverage at
    # all -- only a same-session fake_broker unit test. NOT a candidacy
    # re-opening: AGQ's drought overlay was already rejected on the merits
    # (see docs/research_log.md, uniformly negative at every confirm_days) --
    # these two nodes exist purely to prove the market-buy MECHANISM, not to
    # re-litigate AGQ as a real drought candidate.
    ("AGQ", 86, 157, "v5-overlay-test-d", DROUGHT_CFG),                                  # drought only
    ("AGQ", 86, 157, "v5-overlay-test-da", _merge(DROUGHT_CFG, ADDON_CFG)),               # drought+addon
]


def _get_node(wl_id):
    for row in db.get_watchlist():
        if row["id"] == wl_id:
            return row
    raise ValueError(f"no watch_list row with id={wl_id}")


def _clone(src, version, overlay_cfg, track_label):
    strategy = src["strategy"]
    take_profit = src["arm_sell_pct"] if strategy == "TrailingBothZScoreBreakout" else src["take_profit"]
    label = f"{version} ({track_label}, clone of id={src['id']})"

    db.add_node(
        ticker=src["ticker"], strategy=strategy, version=version, window=src["window"],
        take_profit=take_profit, stop_loss=src["stop_loss"], max_hold_hours=src["max_hold_hours"],
        label=label, z_score_threshold=src["z_score_threshold"], watchlist_id=src["watchlist_id"],
        state="paper", trail_buy_pct=src["trail_buy_pct"], trail_pct=src["trail_sell_pct"],
        entry_timing=src["entry_timing"], starting_notional=src["starting_notional"],
        fixed_sl_override=src["fixed_sl"], account=src.get("account"), paper_role=src.get("paper_role"),
    )

    with db._conn() as c:
        clone_row = c.execute(
            "SELECT id FROM watch_list WHERE ticker=? AND version=? AND COALESCE(paper_role,'')=COALESCE(?,'') "
            "ORDER BY id DESC LIMIT 1",
            (src["ticker"], version, src.get("paper_role")),
        ).fetchone()
        if clone_row is None:
            print(f"  WARNING: no clone found for id={src['id']} ({src['ticker']}, {track_label}, {version})")
            return None
        clone_id = clone_row[0]
        set_clause = ", ".join(f"{k}=?" for k in overlay_cfg)
        c.execute(f"UPDATE watch_list SET {set_clause} WHERE id=?", (*overlay_cfg.values(), clone_id))
        c.commit()
    print(f"  cloned id={src['id']} ({src['ticker']}, {track_label}) -> id={clone_id} [{version}] {overlay_cfg}")
    return clone_id


def main():
    for ticker, live_src_id, daily_src_id, version, overlay_cfg in COMBOS:
        live_src = _get_node(live_src_id)
        _clone(live_src, version, overlay_cfg, "live-track")
        if daily_src_id is not None:
            daily_src = _get_node(daily_src_id)
            _clone(daily_src, version, overlay_cfg, "daily-track")

    print(f"\nStaged test nodes (version LIKE 'v5-overlay-test%'):")
    for row in sorted(db.get_watchlist(), key=lambda r: (r["ticker"], r.get("version") or "", r["id"])):
        if (row.get("version") or "").startswith("v5-overlay-test"):
            role = row.get("paper_role") or "live-track"
            print(f"  id={row['id']:<4} {row['ticker']:<6} {row['version']:<24} {role:<12} "
                  f"drought={row.get('drought_overlay_enabled')} addon={row.get('addon_enabled')} "
                  f"skim={row.get('skim_enabled')}")


if __name__ == "__main__":
    main()
