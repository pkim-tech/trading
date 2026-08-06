"""Creates the paired 'daily-track' (paper_role='daily_sync') node for each v5
watchlist ticker's existing 'live-track' research node -- the second half of the
2026-08-05 two-track paper design (docs/design.md's "Two-account paper
trading" section). Clones the real, current live-track node's config exactly
(not a hardcoded param list -- config may have drifted since the original
build_v5_watchlist.py promotion) rather than re-deriving it, so a daily-track
node is only ever a config-identical sibling that differs solely in paper_role
(which flips its signal-check price source to the last closed hourly bar's
Close, see signals_compute.compute_buy_signal, and makes it subject to the
nightly reconcile classification -- pure observation, no state mutation, see
paper_trading.reconcile_daily_track_nodes).

Filters on version=='v5' specifically, not just (ticker in V5_TICKERS, mode=
'research', no paper_role) -- GDXU has a second research node (version=
'soxl_test', a $500 regression-test pilot) that the looser filter would have
also matched and cloned (found by both paired Opus reviews, 2026-08-05).

Only clones a ticker's node once (existing check_then_skip already inside
add_node handles a rerun no-op'ing cleanly).

Usage:
    .venv/bin/python scripts/add_daily_track_paper_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db

V5_TICKERS = {"AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"}


def main():
    watchlist = db.get_watchlist()
    live_track_nodes = [
        n for n in watchlist
        if n["ticker"] in V5_TICKERS and n.get("mode") == "research" and n.get("version") == "v5"
        and not n.get("paper_role")
    ]
    found = {n["ticker"] for n in live_track_nodes}
    if len(live_track_nodes) != len(V5_TICKERS) or found != V5_TICKERS:
        print(f"WARNING: expected {len(V5_TICKERS)} live-track nodes (version='v5'), found "
              f"{len(live_track_nodes)}: {sorted(found)}. Missing: {sorted(V5_TICKERS - found)}")

    for node in live_track_nodes:
        strategy = node["strategy"]
        take_profit = node["arm_sell_pct"] if strategy == "TrailingBothZScoreBreakout" else node["take_profit"]
        # add_node's K-1/UBTI tax-advantaged-account guard (signals_db.py) is now
        # scoped to mode='live' only (2026-08-05), so a research/paper clone -- this
        # loop always passes mode="research" below -- passes through with its real
        # account tag intact, matching the live-track sibling exactly. AGQ's account
        # is deliberately NOT stripped: the user's explicit call was to keep the
        # 'ira' tag on both nodes ("override agq into the IRA account - we won't put
        # it to live testing there this is paper trade only"), not clone around it.
        account = node.get("account")
        db.add_node(
            ticker=node["ticker"], strategy=strategy, version=node["version"], window=node["window"],
            take_profit=take_profit, stop_loss=node["stop_loss"], max_hold_hours=node["max_hold_hours"],
            label=f"{node.get('label') or ''} (daily-track)".strip(),
            z_score_threshold=node["z_score_threshold"], watchlist_id=node["watchlist_id"], mode="research",
            trail_buy_pct=node["trail_buy_pct"], trail_pct=node["trail_sell_pct"],
            entry_timing=node["entry_timing"], starting_notional=node["starting_notional"],
            fixed_sl_override=node["fixed_sl"], account=account, paper_role="daily_sync",
        )
        # add_node()'s signature has no params for the 2026-08-09 drought/addon/
        # skim overlay columns -- without this direct sync, a daily-track clone
        # would silently be created WITHOUT its live-track sibling's overlay
        # config, defeating the whole point of a config-identical pair (found
        # while building task 6 of the overlay checklist; not yet a real gap
        # today since no live-track node has any of these enabled, but it would
        # have silently bitten the first node that ever does).
        with db._conn() as c:
            clone_row = c.execute(
                "SELECT id FROM watch_list WHERE ticker=? AND paper_role='daily_sync' "
                "AND watchlist_id=? ORDER BY id DESC LIMIT 1",
                (node["ticker"], node["watchlist_id"])
            ).fetchone()
            if clone_row is None:
                # add_node() silently no-op'd (e.g. a dedup match against an
                # already-existing row) rather than raising -- found by
                # review, 2026-08-09: the original unguarded fetchone()[0]
                # would crash with a confusing TypeError here instead of a
                # clear message.
                print(f"  WARNING: no daily-track clone found for {node['ticker']} after add_node() -- "
                      f"skipping overlay-config sync for it")
                continue
            clone_id = clone_row[0]
            c.execute("""
                UPDATE watch_list SET
                    drought_overlay_enabled=?, drought_confirm_days=?, drought_vol_gate=?,
                    drought_sl_pct_override=?, drought_arm_pct_override=?, drought_trail_pct_override=?,
                    addon_enabled=?, skim_enabled=?, skim_step=?, skim_frac=?
                WHERE id=?
            """, (
                node.get("drought_overlay_enabled", 0), node.get("drought_confirm_days"),
                node.get("drought_vol_gate"), node.get("drought_sl_pct_override"),
                node.get("drought_arm_pct_override"), node.get("drought_trail_pct_override"),
                node.get("addon_enabled", 0), node.get("skim_enabled", 0),
                node.get("skim_step"), node.get("skim_frac"), clone_id,
            ))
            c.commit()
        print(f"  cloned {node['ticker']} ({strategy[:20]}) -> daily-track (daily_sync)")

    print("\nDaily-track nodes now on watchlist:")
    for row in db.get_watchlist():
        if row.get("paper_role") == "daily_sync":
            print(f"  id={row['id']:<4} {row['ticker']:<6} {row['strategy']}")


if __name__ == "__main__":
    main()
