"""Creates the dry-run canary nodes for real drought/addon order-placement
code (docs/plans/real_order_execution_drought_addon.md's Part 12
prerequisite: "run the entire mechanism in dry_run=True on brokerage
(margin-typed, dry-run) for a full cycle first"), agreed 2026-08-1x after the
17+4-node paper staging matrix was judged sufficient to move to this next
real layer.

Unlike stage_overlay_test_nodes.py (paper, mode='research'), these are
mode='live' on the 'brokerage' account -- the one margin-typed, dry_run=True
account, so every real check_order guard actually runs (including is_addon_
leg's five preconditions) with schwab_client.place_equity_buy/place_trailing_
buy short-circuiting before the real broker submission. This is a genuine
rehearsal of the real code, not a simulation of it -- zero real capital risk
regardless of outcome (dry_run confirmed unconditional at every real
placement call site in schwab_client.py).

**4 DISTINCT tickers, not 1** -- REVISED 2026-08-1x after a paired Opus
review (independent-cold + contextual) found the original all-SOXL design
broken: schwab_safety.get_watch_list_node(ticker, account) returns None on
ANY 2+ match (ticker+account, not wl_id-keyed), so 4 nodes sharing SOXL/
brokerage made _node_id always None for all of them -- node_automation_
enabled(None) defaults True (per-node pause silently inert), the existing-
position guard's own documented "not reachable today (verified: no such
pairing exists)" limitation became reachable four ways over (only the FIRST
node to get a position could ever enter; the other 3 would be refused as
duplicates), and is_addon_leg's five preconditions could validate against
the WRONG node's position entirely. None of that reflects real deployment
(one node per ticker+account in production) and none of it proves anything
about the mechanisms under test -- the opposite of the point of staging.
Distinct tickers restore exact real-deployment shape (get_watch_list_node
resolves uniquely per node) with zero loss of coverage, since every
combo's real params are cloned from that ticker's own real v5 node.

  - v5-canary-drought          : SOXL, TrailingBothZScoreBreakout (trailing-buy entry,
                                  SOXL's real live strategy) -- drought entry -> HANDOFF.
  - v5-canary-drought-marketbuy: AGQ, TrailingExitZScoreBreakout (market-buy entry) --
                                  proves drought's OTHER real entry-dispatch branch
                                  (notify_drought_buy_signal's _attempt_automated_market_buy
                                  path) organically live. AGQ is the real ticker that
                                  actually runs this strategy in production (unlike the
                                  original design's SOXL-with-a-foreign-strategy choice) --
                                  same rationale as the AGQ drought/drought+addon PAPER
                                  nodes added the same session (see
                                  stage_overlay_test_nodes.py), now extended one layer
                                  deeper. AGQ's drought overlay was already rejected on
                                  the merits for REAL trading (docs/research_log.md,
                                  uniformly negative at every confirm_days) -- this node
                                  exists purely to prove the mechanism fires correctly,
                                  not to reconsider that rejection.
  - v5-canary-addon            : HIBL, TrailingBothZScoreBreakout -- addon entry ->
                                  lockstep exit. Addon's own mechanism (check_addon_
                                  trigger_real/close_addon_leg_real_if_open) doesn't
                                  depend on entry mechanism at all (confirmed by reading
                                  strategies.py: both TrailingBoth/TrailingExit share
                                  byte-identical arm/trailing-exit logic), so no market-
                                  buy cross needed here -- HIBL is just a 2nd real
                                  TrailingBoth ticker, chosen only to keep this node's
                                  ticker distinct from the other 3.
  - v5-canary-drought-addon    : USD, TrailingBothZScoreBreakout -- both together,
                                  a 3rd distinct real TrailingBoth ticker.

Every one of these tickers is already in AUTOMATION_ENABLED_TICKERS. None of
the 4 chosen tickers have an existing watchlist_id=65 (the active watchlist)
node on 'brokerage' -- the only pre-existing 'brokerage' nodes (TQQQ/AGQ/
GDXU/DPST, ids 40-54) all sit on archived watchlists 7/9, which schwab_
safety's get_watch_list_node calls don't see (they default to the active
watchlist), so there's no collision with them. AGQ's node id=41 (mode='live',
archived watchlist) does still share 'brokerage' account for daily_order_cap
purposes (BUY-side, account-wide, not watchlist-scoped) -- see the cap bump
in this script's __main__ note below.

Small notional ($1,000) on every node -- dry_run means no real capital ever
moves, but realistic-small sizing keeps share-count math sane for review.

Usage:
    .venv/bin/python scripts/stage_dry_run_canary_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db

ACCOUNT = "brokerage"
NOTIONAL = 1000

DROUGHT_CFG = {"drought_overlay_enabled": 1, "drought_confirm_days": 3, "drought_vol_gate": 0.4}
ADDON_CFG = {"addon_enabled": 1}


def _merge(*cfgs):
    out = {}
    for c in cfgs:
        out.update(c)
    return out


def _get_node(wl_id):
    for row in db.get_watchlist():
        if row["id"] == wl_id:
            return row
    raise ValueError(f"no watch_list row with id={wl_id}")


def _stage(src, version, overlay_cfg):
    strategy = src["strategy"]
    take_profit = src["arm_sell_pct"] if strategy == "TrailingBothZScoreBreakout" else src["take_profit"]
    trail_buy_pct = src["trail_buy_pct"] if strategy == "TrailingBothZScoreBreakout" else None

    label = f"{version} (dry-run canary, clone of id={src['id']})"
    db.add_node(
        ticker=src["ticker"], strategy=strategy, version=version, window=src["window"],
        take_profit=take_profit, stop_loss=src["stop_loss"], max_hold_hours=src["max_hold_hours"],
        label=label, z_score_threshold=src["z_score_threshold"], watchlist_id=src["watchlist_id"],
        state="live", trail_buy_pct=trail_buy_pct, trail_pct=src["trail_sell_pct"],
        entry_timing=src["entry_timing"], starting_notional=NOTIONAL,
        fixed_sl_override=src["fixed_sl"], account=ACCOUNT,
    )

    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM watch_list WHERE ticker=? AND strategy=? AND version=? AND account=? "
            "ORDER BY id DESC LIMIT 1",
            (src["ticker"], strategy, version, ACCOUNT),
        ).fetchone()
        if row is None:
            print(f"  WARNING: no node found after add_node for {version}/{strategy}")
            return None
        node_id = row[0]
        if overlay_cfg:
            set_clause = ", ".join(f"{k}=?" for k in overlay_cfg)
            c.execute(f"UPDATE watch_list SET {set_clause} WHERE id=?", (*overlay_cfg.values(), node_id))
            c.commit()
    print(f"  staged id={node_id} [{version}] {src['ticker']} {strategy} {overlay_cfg}")
    return node_id


# (source_real_v5_node_id, version, overlay_cfg)
COMBOS = [
    (92, "v5-canary-drought", DROUGHT_CFG),                   # SOXL, TrailingBoth
    (86, "v5-canary-drought-marketbuy", DROUGHT_CFG),         # AGQ, TrailingExit
    (89, "v5-canary-addon", ADDON_CFG),                       # HIBL, TrailingBoth
    (94, "v5-canary-drought-addon", _merge(DROUGHT_CFG, ADDON_CFG)),  # USD, TrailingBoth
]


def main():
    for src_id, version, overlay_cfg in COMBOS:
        _stage(_get_node(src_id), version, overlay_cfg)

    print(f"\nStaged dry-run canary nodes (version LIKE 'v5-canary%'):")
    for row in sorted(db.get_watchlist(), key=lambda r: (r.get("version") or "", r["id"])):
        if (row.get("version") or "").startswith("v5-canary"):
            print(f"  id={row['id']:<4} {row['ticker']:<6} {row['strategy']:<28} {row['version']:<28} "
                  f"account={row.get('account')} state={row.get('state')} "
                  f"drought={row.get('drought_overlay_enabled')} addon={row.get('addon_enabled')}")

    print(f"\nNOTE: 'brokerage' account's daily_order_cap is 5 (schwab_safety.ACCOUNTS) -- "
          f"shared account-wide (BUY-side) across these 4 new nodes plus the pre-existing "
          f"live AGQ node (id=41, archived watchlist). 4 nodes x up to ~3 real BUY attempts "
          f"each (core entry + drought entry + addon entry) on a busy day can plausibly "
          f"exceed 5 -- consider raising the cap if a real rehearsal day hits it (a false "
          f"daily_order_cap block is indistinguishable in the coverage log from a genuine "
          f"guard finding). Not changed automatically here -- account-limit changes are a "
          f"deliberate, separate decision.")


if __name__ == "__main__":
    main()
