"""Evening account check-in (docs/plans/account_checkin_process.md). 4 parts, callable individually.

Usage:
  python scripts/evening_status.py 1   # log warnings, today's trading hours
  python scripts/evening_status.py 2   # real capital-at-stake node states
  python scripts/evening_status.py 3   # trades-vs-kernel + unexplained deviations
  python scripts/evening_status.py 4   # readiness for tomorrow
  python scripts/evening_status.py all
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_db as db
import signals_helpers as helpers
import scripts.verify_real_trades_vs_kernel as verify
import scripts.daemon_status as daemon_status

TODAY = datetime.now().strftime('%Y-%m-%d')


def part1():
    print(f"=== Part 1: log warnings ({TODAY} 09:30-16:00 ET) ===")
    log = Path("logs/active_signals.log")
    if not log.exists():
        print("no log file")
        return
    hits = []
    for line in log.read_text(errors='ignore').splitlines():
        if not line.startswith(f"{TODAY} 09:3") and not any(
                line.startswith(f"{TODAY} {h:02d}:") for h in range(10, 16)):
            continue
        if '⚠️' in line and 'schwab_stream' not in line:
            hits.append(line)
    print(f"{len(hits)} warning(s)" if hits else "clean, no warnings")
    for h in hits:
        print(f"  {h}")


def part2():
    print(f"=== Part 2: real capital-at-stake nodes ({TODAY}) ===")
    live_nodes = [n for n in db.get_watchlist(False) or [] if n.get('state') == 'live']
    if not live_nodes:
        # get_watchlist(False) may not support cross-watchlist search -- fall back to a direct scan.
        import sqlite3
        con = sqlite3.connect("cache/live/trading_live.db")
        con.row_factory = sqlite3.Row
        live_nodes = [dict(r) for r in con.execute("SELECT * FROM watch_list WHERE state='live'")]
        con.close()
    nodes = [n for n in live_nodes if helpers.has_capital_at_stake(n)]

    rows = []
    for n in nodes:
        sig = a.compute_buy_signal(n)
        if sig is None:
            continue
        cur = sig['current_price']
        state = db.get_real_position_state(n['id'])
        pending = state['pending_buy']
        if pending is not None:
            _, tb_trigger = a._trailing_buy_status(pending)
            trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
            status = f"pending_entry, {pending['signal_time'][5:16]}"
        elif state['status'] == 'holding':
            trigger = sig['lower_band']
            status = f"holding, {state['real_position']['shares']}sh"
        else:
            trigger = sig['lower_band']
            status = "flat"
        pct = (cur - trigger) / trigger * 100
        rows.append((n['ticker'], n['account'], n.get('starting_notional', 0), trigger, cur, pct, status))
    rows.sort(key=lambda r: r[5])

    print(f"{'Ticker':6s} {'Acct':10s} {'Notional':>9s} {'Trigger':>9s} {'Current':>9s} {'%':>8s}  State")
    for t, acct, notional, trig, cur, pct, status in rows:
        print(f"{t:6s} {acct or '':10s} ${notional:>7,.0f} {trig:>9.2f} {cur:>9.2f} {pct:>7.2f}%  {status}")


def part3():
    print(f"=== Part 3: trades vs kernel, coverage deviations ({TODAY}) ===")
    real = verify.get_real_trades(TODAY, TODAY, accounts=None)
    real = [r for r in real if r['wl_id'] and r['wl_id'] > 0]
    wl_ids = sorted({r['wl_id'] for r in real})
    nodes, skipped = verify.resolve_nodes(wl_ids, min_notional=5000)
    if not nodes:
        print("no real capital-at-stake trades today")
    for wl_id, node in nodes.items():
        node_real = [r for r in real if r['wl_id'] == wl_id]
        bt = verify.get_backtest_trades_in_window(node, TODAY, TODAY)
        matched, unmatched_real, unmatched_bt = verify.match_trades(
            [{"signal_time": r["signal_time"], "entry_time": r["entry_time"],
              "ticker": r["ticker"], "exit_reason": r["exit_reason"]} for r in node_real],
            [{"entry_time": str(t["entry_time"])} for t in bt], 4)
        genuine = [r for r in unmatched_real if not verify.is_staged_or_manual(r['ticker'], r['entry_time'], r['exit_reason'])]
        flag = f"UNEXPLAINED ({len(genuine)})" if genuine else "matches kernel"
        print(f"  {node['ticker']:6s} {node['account'] or '':10s} wl_id={wl_id:4d}  {flag}")

    devs = [d for d in db.get_deviations(unexplained_only=True) if d.get('check_date') == TODAY]
    print(f"\n{len(devs)} unexplained coverage_deviation(s) today")
    for d in devs:
        print(f"  {d['scenario_key']:28s} {d['ticker'] or '':6s} {d['actual_summary']}")


def part4():
    print("=== Part 4: readiness for tomorrow ===")
    pid = daemon_status._find_daemon_pid()
    if not pid:
        print("daemon: NOT RUNNING")
    else:
        start_epoch = int(subprocess.run(["stat", "-c", "%Y", f"/proc/{pid}"], capture_output=True, text=True).stdout.strip())
        newest_mtime = max((Path(f).stat().st_mtime for f in daemon_status.LIVE_SOURCE_FILES if Path(f).exists()), default=0)
        stale = newest_mtime > start_epoch
        print(f"daemon: RUNNING (pid {pid}), {'STALE -- restart to pick up code changes' if stale else 'current'}")

    incidents = [i for i in db.get_incidents() if i.get('resolved_ts') is None]
    print(f"\n{len(incidents)} open trading_incident(s)")
    for i in incidents:
        print(f"  #{i['id']} {i['ts']} — {i.get('title', '')}")


PARTS = {'1': part1, '2': part2, '3': part3, '4': part4}


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if arg == 'all':
        for p in PARTS.values():
            p()
            print()
    elif arg in PARTS:
        PARTS[arg]()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
