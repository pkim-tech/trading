"""On-demand ground-truth sweep: does the broker hold any real position with
NO matching open_positions row at all, in ANY account, regardless of what
any node's `state` currently says?

Built 2026-08-07 after a real, live incident: GDXU (soxl_ira) sat as a real,
unprotected broker position for a week with zero local record. Every
existing check (signals_invariants, check_live_state_reconciliation,
coverage_registry) is DB-driven -- it verifies local records against
themselves or against a committed baseline, but nothing ever asked the
broker "what do you actually hold" and swept for anything with no local
match at all. This is that sweep -- see automation_principles.md #1
("Reconfirm real state before acting -- never trust a local/cached record
as ground truth") and schwab_client.get_all_real_positions's docstring.

Deliberately on-demand only, not periodic/scheduled -- run it whenever you
want a real answer to "what's actually open," not on a timer (explicit user
call, 2026-08-06: "when I ask you for state, I expect state").

Also flags the mirror-image drift: a real open_positions row (is_dry_run_sim=0)
whose ticker the broker no longer holds at all, or holds a different share
count for -- a position that closed/changed at the broker without the local
record catching up.

A real holding whose ticker has NEVER appeared in this account's watch_list
(e.g. the user's own hand-held, non-algo investments) is printed as
informational only, not flagged as UNTRACKED -- this tool exists to catch a
real ALGO position falling through the cracks, not to demand every manual
holding be registered. Only a ticker that IS/WAS part of this trading
system's watch_list for that account, with no matching open_positions row,
is a real finding.

Usage: .venv/bin/python scripts/check_untracked_positions.py [--account NAME ...]
       (default: every account in schwab_safety.ACCOUNTS, plus any other
       account with a resolvable real hash -- see docstring on account scope
       below for why both sources matter)
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab_client
import schwab_safety
import signals_db as db


def _known_tickers(account):
    """Every ticker that has ever had a watch_list node for this account --
    the algo-relevant universe. A real broker holding outside this set is a
    hand-held, non-algo position (this project's own convention: several
    accounts carry manual investments alongside the algo nodes) and must not
    render as an UNTRACKED finding just because this tool doesn't know about
    it -- found by paired Opus review: an earlier version flagged every
    hand-held holding in every real account, permanently red, training the
    operator to ignore the one tool built to catch a genuine gap."""
    with db._conn() as c:
        rows = c.execute("SELECT DISTINCT ticker FROM watch_list WHERE account=?", (account,)).fetchall()
    return {r["ticker"] for r in rows}


def check_account(account):
    print(f"\n=== {account} ===")
    try:
        hashes = schwab_client._resolve_account_hashes()
    except Exception as e:
        print(f"  [error] couldn't resolve account hashes: {e}")
        return [f"[{account}] couldn't resolve account hashes: {e}"]
    if account not in hashes:
        print(f"  [skip] not linked (no SCHWAB_ACCOUNT_{account.upper()} match)")
        return []

    try:
        real_positions = schwab_client.get_all_real_positions(account)
        real_shorts = schwab_client.get_all_real_short_positions(account)
    except Exception as e:
        # A fetch failure must count as a finding, not a silent "clean" --
        # this tool's whole purpose is ground truth; "couldn't ask" is not
        # the same as "nothing wrong" (found by paired Opus review: the
        # original version returned [] here, which main() then treated as
        # all-clear -- a token expiry or transient outage across every
        # account would have printed per-account [error] lines followed by
        # "No untracked/mismatched/stale positions found").
        msg = f"couldn't fetch real positions: {e}"
        print(f"  [error] {msg}")
        return [f"[{account}] {msg}"]

    known = _known_tickers(account)

    with db._conn() as c:
        # Sum shares per ticker, not last-row-wins -- two nodes can
        # legitimately share a ticker in one account (this project has run
        # exactly that configuration, e.g. two differently-parameterized
        # GDXU nodes). Also folds in any open, real (is_dry_run_sim=0)
        # add-on leg's shares -- check_live_state_reconciliation treats this
        # as REQUIRED, not optional, for the identical broker-vs-local
        # comparison: with a real leg open, the broker legitimately holds
        # core + leg shares for the same ticker/account.
        local_rows = defaultdict(float)
        for r in c.execute(
            "SELECT ticker, shares FROM open_positions WHERE account=? AND is_dry_run_sim=0", (account,)
        ).fetchall():
            local_rows[r["ticker"]] += r["shares"] or 0.0
        for r in c.execute(
            "SELECT ticker, shares FROM addon_legs WHERE account=? AND is_dry_run_sim=0 AND status='open'",
            (account,),
        ).fetchall():
            local_rows[r["ticker"]] += r["shares"] or 0.0

    findings = []
    informational = []

    for ticker, real_long in real_positions.items():
        local_shares = local_rows.get(ticker)
        if local_shares is None:
            if ticker in known:
                findings.append(f"  \U0001F6A8 UNTRACKED: broker holds {real_long:g} {ticker}, "
                                 f"NO open_positions/addon_legs row exists at all (ticker IS in this "
                                 f"account's watch_list)")
            else:
                informational.append(f"  ℹ️  hand-held: broker holds {real_long:g} {ticker}, "
                                      f"never in this account's watch_list -- not flagged")
        elif abs(local_shares - real_long) > 1e-6:
            findings.append(f"  ⚠️  MISMATCH: {ticker} -- broker={real_long:g} shares, "
                             f"local (open_positions+open addon legs)={local_shares:g} shares")

    for ticker, local_shares in local_rows.items():
        if ticker not in real_positions:
            findings.append(f"  ⚠️  STALE: local says {local_shares:g} {ticker}, broker holds 0 long "
                             f"(closed at the broker without local catching up, or a data error)")

    for ticker, short_qty in real_shorts.items():
        # The most dangerous untracked state a "ground truth" sweep could
        # miss -- an accidental naked short (exactly what live_sanity_check.py's
        # naked-SELL test exists to guard against). Always a finding,
        # regardless of the known-tickers filter.
        findings.append(f"  \U0001F6A8 SHORT: broker holds a SHORT position of {short_qty:g} {ticker} "
                         f"-- verify this is expected, not an oversell")

    if informational:
        for i in informational:
            print(i)
    if not findings:
        print(f"  clean -- {len(real_positions)} real long position(s), {len(real_shorts)} short(s), "
              f"{len(local_rows)} local row(s) (incl. open addon legs), all matched")
    else:
        for f in findings:
            print(f)
    return findings


def _check_null_account_rows():
    """Real (is_dry_run_sim=0) open_positions rows with account IS NULL are
    invisible to the per-account sweep above (it only ever queries a specific
    account value) -- run once, separately, regardless of --account scope.
    signals_invariants.py has a dedicated check for a state='live' node with
    account=None; this is the position-side mirror of that same real gap."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT ticker, shares FROM open_positions WHERE account IS NULL AND is_dry_run_sim=0"
        ).fetchall()
    if not rows:
        return []
    print("\n=== NULL-account real positions (can't be swept against any broker account) ===")
    findings = []
    for r in rows:
        msg = f"  \U0001F6A8 {r['ticker']}: {r['shares']:g} shares, account=NULL -- cannot verify against any broker"
        print(msg)
        findings.append(msg)
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", nargs="+", help="specific account(s); default: every account in "
                     "schwab_safety.ACCOUNTS plus any other linked/resolvable account")
    args = ap.parse_args()

    if args.account:
        accounts = args.account
    else:
        # Union, not just schwab_safety.ACCOUNTS.keys() -- a linked account
        # that isn't in ACCOUNTS yet (e.g. a newly-opened one, still pending
        # compliance/token setup) is real and holds real shares, but was
        # previously invisible to this sweep entirely. Found by paired Opus
        # review.
        try:
            linked = set(schwab_client._resolve_account_hashes().keys())
        except Exception:
            linked = set()
        accounts = sorted(set(schwab_safety.ACCOUNTS.keys()) | linked)

    all_findings = {}
    for account in accounts:
        findings = check_account(account)
        if findings:
            all_findings[account] = findings

    null_findings = _check_null_account_rows()
    if null_findings:
        all_findings["(NULL account)"] = null_findings

    print(f"\n{'='*60}")
    if all_findings:
        total = sum(len(f) for f in all_findings.values())
        print(f"{total} finding(s) across {len(all_findings)} account(s) -- see above.")
        sys.exit(1)
    else:
        print("No untracked/mismatched/stale positions found across all checked accounts.")


if __name__ == "__main__":
    main()
