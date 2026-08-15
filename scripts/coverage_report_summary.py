"""Composition layer for the nightly Slack "Coverage Report" (signals_notify.
send_coverage_report, posted at the 16:05 ET EOD slot).

Reporting only -- nothing here decides what counts as a deviation, records a
coverage_deviations row, or changes scripts/coverage_check.py's status
computation. It takes run_check()'s own live return value plus read-only
trade_log/open_positions/trading_incidents queries and decides what a human
glancing at their phone actually sees.

Why this exists (2026-08-14, direct user complaint): the report had grown to
~200 lines that were 100% canary/test-infrastructure -- ~27 per-ticker
`canary_<scenario>` status lines plus ~20 `reconciliation_mismatch`
"informational, no ticket" lines -- while the two things the user actually
checks at end of day ("did I make or lose money today", "did something break")
were absent entirely. The canary breakdown is real proof, just not news every
night; it collapses to one rollup line here and stays fully available on demand
via `.venv/bin/python scripts/coverage_check.py [--date ...]` (unchanged) and
`scripts/coverage_matrix.py`.

One correction to the original framing worth recording: the
`reconciliation_mismatch` rows are NOT canary-only. 20 active
scenario_expectations rows carry that key and 7 of them point at real soxl_ira
nodes (SH/SPY/DPST/ERY/LABD/RETL/GDXU), matching
scripts/coverage_registry.py's `live_state_reconciliation_mismatch` row, whose
own notes cite 8 real soxl_ira detections. They're collapsed here for the same
reason as canary (informational tier, never mints a ticket -- see
coverage_check.run_check's 'informational' handling), but they're labelled as
their own non-canary bucket rather than folded into a line calling them canary.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def _key(row_or_result):
    """Same 4-tuple identity coverage_check/send_coverage_report use: scenario_key
    alone is not unique (two active rows can share one key, disambiguated by
    node_id/mode -- Opus review 2026-07-25)."""
    return (row_or_result['scenario_key'], row_or_result.get('ticker') or '',
            row_or_result.get('node_id'), row_or_result.get('mode') or '')


def classify(results, reasons):
    """Buckets run_check results into per-group counts. Group is 'canary' for a
    canary_* scenario_key, 'other' for everything else (today: only
    reconciliation_mismatch reaches the daily/informational check besides
    canary). Status buckets mirror send_coverage_report's own per-line logic
    exactly -- met / skipped / informational (a miss that by design mints no
    ticket) / explained / unexplained -- so the rollup counts can't drift from
    what a full per-scenario render would have shown."""
    groups = {}
    for r in results:
        group = 'canary' if (r['scenario_key'] or '').startswith('canary_') else 'other'
        g = groups.setdefault(group, dict(total=0, met=0, skipped=0, informational=0,
                                          explained=0, unexplained=0, keys=set()))
        g['total'] += 1
        g['keys'].add(r['scenario_key'])
        if r['status'] == 'met':
            g['met'] += 1
        elif r['status'] == 'skipped':
            g['skipped'] += 1
        elif not r.get('ticket_eligible', True):
            g['informational'] += 1
        elif reasons.get(_key(r)):
            g['explained'] += 1
        else:
            g['unexplained'] += 1
    return groups


def _node_row(node_id):
    if node_id is None:
        return None
    with db._conn() as c:
        r = c.execute("SELECT version, state FROM watch_list WHERE id = ?", (node_id,)).fetchone()
    return dict(r) if r else None


def is_canary_result(result):
    """Is this scenario row about a test/canary node rather than real money?

    Deliberately a DIFFERENT question from classify()'s 'canary' vs 'other'
    grouping above, which buckets by scenario FAMILY (scenario_key prefix) for
    the rollup. That grouping is right for the rollup and wrong here: the real
    2026-08-14 `scenario_expectations` set has 19 active `reconciliation_
    mismatch` rows, 13 of them on canary/dry_run nodes and 6 on real ones
    (soxl_test/v4/v5, 3 of those `state='live'`), so a scenario_key prefix
    cannot answer 'is real money involved'. Answering it per NODE is the whole
    point of the split: a canary deviation is structurally expected most
    nights, a real node's unexplained deviation should never happen.

    Canary = the node's `version` mentions canary, or it's `state='dry_run'`
    (no real order ever reaches the broker, so no real money is at stake
    either way). Falls back to the scenario_key prefix when the node can't be
    resolved, and treats anything still unknown as REAL -- the conservative
    direction, since the failure mode to avoid is a real-money deviation
    hiding inside the canary bucket."""
    node = _node_row(result.get('node_id'))
    if node:
        return 'canary' in (node.get('version') or '').lower() or node.get('state') == 'dry_run'
    return (result.get('scenario_key') or '').startswith('canary_')


def split_unexplained(unexplained):
    """(real_node_rows, canary_node_rows), order within each preserved."""
    real, canary = [], []
    for r in unexplained:
        (canary if is_canary_result(r) else real).append(r)
    return real, canary


def _group_label(group, counts):
    if group == 'canary':
        return 'Canary'
    keys = sorted(counts['keys'])
    if len(keys) == 1:
        return keys[0]
    return 'Other scenarios'


def rollup_lines(results, reasons):
    """One line per group, replacing the old per-scenario block (~47 lines
    against the real 2026-08-14 scenario set)."""
    groups = classify(results, reasons)
    lines = []
    for group in ('canary', 'other'):
        counts = groups.get(group)
        if not counts:
            continue
        # Ordered worst-first: on a phone the first few words are all that's
        # read. The met fraction is dropped for a group that is entirely
        # informational (reconciliation_mismatch's real shape today) -- "0/20
        # met" reads like a failure when it means "no mismatch was detected,
        # which is the good outcome and never mints a ticket".
        parts = []
        if counts['unexplained']:
            parts.append(f"{counts['unexplained']} UNEXPLAINED")
        if counts['explained']:
            parts.append(f"{counts['explained']} explained")
        if not (counts['met'] == 0 and counts['informational'] == counts['total']):
            parts.append(f"{counts['met']}/{counts['total']} met")
        if counts['informational']:
            parts.append(f"{counts['informational']} informational (no ticket)")
        if counts['skipped']:
            parts.append(f"{counts['skipped']} not checked")
        lines.append(f"{_group_label(group, counts)}: {', '.join(parts)}")
    return lines


def _real(rows):
    """Real money only: paper lives in its own tables, and a dry_run/canary node's
    synthesized fill is tagged is_dry_run_sim=1 on the real tables (2026-07-26
    design)."""
    return [r for r in rows if not r.get('is_dry_run_sim')]


def todays_pnl(check_date, price_fn=None):
    """P&L rollup: realized $ on real trades closed on check_date, plus
    unrealized $ on every real open position right now.

    NOT a same-day mark-to-market, and the report must not call it one (Opus
    review, 2026-08-14 -- the line used to be labelled 'today'). Both halves
    are lifetime-to-date figures:
      * realized = each trade's FULL entry-to-exit P&L, for trades that merely
        happen to have exited on check_date. A position entered three days ago
        and closed today books all three days' move here.
      * unrealized = each open position's total move since ITS OWN entry, not
        today's price change. A position open since last week shows the whole
        week's gain.
    Making either genuinely same-day needs a prior-close mark per ticker, which
    this module has no cheap, trustworthy source for; the dollar arithmetic
    itself is correct, so the labelling is what got fixed instead.

    Deliberately NOT the calendar-year/tax-aware portfolio-return calculator
    that's a separate open backlog item (raised 2026-08-13/14) -- this is
    shares x price arithmetic off trade_log/open_positions, nothing more.
    Returns counts of positions whose price fetch failed rather than guessing,
    so the report can say so instead of quietly understating the number."""
    if price_fn is None:
        import schwab_client
        price_fn = schwab_client.get_current_price

    realized = 0.0
    closed = _real(db.get_trades_closed_on_date(check_date))
    for t in closed:
        if t.get('shares') and t.get('entry_price') is not None and t.get('exit_price') is not None:
            realized += t['shares'] * (t['exit_price'] - t['entry_price'])

    unrealized = 0.0
    priced = 0
    unpriced = 0
    for p in _real(db.get_open_positions()):
        if not p.get('shares') or p.get('entry_price') is None:
            continue
        try:
            px = price_fn(p['ticker'])
        except Exception:
            px = None
        if px is None:
            unpriced += 1
            continue
        unrealized += p['shares'] * (px - p['entry_price'])
        priced += 1
    return dict(realized=realized, unrealized=unrealized, closed=len(closed),
                open_priced=priced, open_unpriced=unpriced)


def _money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _trade_log_shares_fix_landed():
    """Whether signals_db.log_trade_exit accepts a `shares` kwarg -- the actual
    Tranche 1 fix for the top-up-staleness defect below, built in a parallel
    worktree/tranche this module can't assume is merged. Checked by
    introspection rather than hardcoded (2026-08-15 review finding, F1): an
    earlier version of this module's docstring/caveat text flatly asserted
    "the fix" exists, which was true in the tranche that wrote it but false in
    this module's own tree at the time -- a caveat that overclaims a fix is as
    misleading as one that doesn't mention the risk at all. Never raises."""
    try:
        import inspect
        return 'shares' in inspect.signature(db.log_trade_exit).parameters
    except Exception:
        return False


def stale_share_count_tickers(check_date):
    """Tickers among check_date's closed trades whose `trade_log.shares` may be
    understated because a same-day post-fill top-up was placed during the
    position's life.

    Known real defect: `_reconcile_fill`'s top-up adds real shares to the
    broker position, and the trade_log row's `shares` didn't always follow --
    see `_trade_log_shares_fix_landed()` for whether this tree has the fix.
    Even once fixed, historical rows predating it (RETL's 2026-08-10 top-up is
    the known real one) stay understated -- there's no backfill. Rather than
    silently trusting the number, the report names the ticker when a `top_up`
    `result='placed'` event exists between that trade's entry and exit dates.

    coverage_events.ts is UTC (datetime('now')) while entry/exit dates are
    local, so the date is converted in SQL, same as incident_lines."""
    closed = _real(db.get_trades_closed_on_date(check_date))
    if not closed:
        return []
    with db._conn() as c:
        topups = [(r[0], r[1]) for r in c.execute(
            "SELECT ticker, date(ts, 'localtime') FROM coverage_events "
            "WHERE scenario_key = 'top_up' AND result = 'placed'").fetchall()]
    flagged = []
    for t in closed:
        entry_day = (t.get('entry_time') or '')[:10]
        exit_day = (t.get('exit_time') or '')[:10]
        for ticker, day in topups:
            if ticker == t.get('ticker') and entry_day <= day <= exit_day:
                if t['ticker'] not in flagged:
                    flagged.append(t['ticker'])
                break
    return flagged


def pnl_line(check_date, price_fn=None, pnl=None):
    """The money line. Every figure here is lifetime-to-date, never today-only
    -- see todays_pnl's docstring. The wording is load-bearing: the previous
    '*Portfolio: X today*' was a factually wrong claim about what was measured,
    not just a loose phrasing."""
    if pnl is None:
        pnl = todays_pnl(check_date, price_fn=price_fn)
    total = pnl['realized'] + pnl['unrealized']
    open_bit = (f"unrealized {_money(pnl['unrealized'])} on {pnl['open_priced']} open "
                f"(total since entry, not today's move)")
    if pnl['open_unpriced']:
        open_bit += f", {pnl['open_unpriced']} unpriced"
    line = (f"*Portfolio: {_money(total)} realized+open* "
            f"(realized {_money(pnl['realized'])} on {pnl['closed']} closed today "
            f"— each trade's full lifetime P&L, not today's move; {open_bit})")
    try:
        stale = stale_share_count_tickers(check_date)
    except Exception:
        stale = []
    if stale:
        if _trade_log_shares_fix_landed():
            reason = "trade_log.shares predates the fix for that"
        else:
            # 2026-08-15 review finding (F1): don't claim a fix exists that this
            # tree doesn't actually have -- this module can run in a worktree
            # where the Tranche 1 fix hasn't landed yet.
            reason = ("a known trade_log.shares staleness defect is not fixed in this "
                       "tree yet — see trading_incidents")
        line += (f"\n  ⚠️ realized may be understated for {', '.join(stale)}: a post-fill "
                 f"top-up was placed during the position's life and {reason}")
    return line


SUB_THRESHOLD_NOTIONAL = 5000.0


def sub_threshold_real_nodes(threshold=SUB_THRESHOLD_NOTIONAL):
    """Real (state='live') nodes sized below `threshold` -- today's soxl_ira tier.

    state='live' is what separates real order placement from the 55 dry_run
    canary nodes, and the notional basis is MAX(static starting_notional, real
    effective notional) exactly as signals_helpers.has_capital_at_stake
    computes it, so this can't disagree with that gate about a node's real
    size. Different threshold on purpose: has_capital_at_stake asks "is this
    big enough to alert on in real time" ($10k default); this asks "is this the
    small-but-real tier the nightly summary is about" ($5k, user's number)."""
    import signals_helpers as helpers
    nodes = []
    for n in _live_nodes():
        basis = n.get('starting_notional') or 0.0
        try:
            basis = max(basis, helpers._last_sale_recovery(n) or 0.0)
        except Exception:
            pass
        if basis < threshold:
            nodes.append(n)
    return nodes


def _live_nodes():
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM watch_list WHERE state='live'")]


def slack_volume_line(days=7, threshold=SUB_THRESHOLD_NOTIONAL):
    """Raw count of Slack messages in the trailing `days` that mention a
    sub-threshold real node's ticker -- deliberately just a number.

    A review TRIGGER, not a judgment: a script can count volume but cannot
    decide whether the volume is annoying, so there's no threshold/flag logic
    here on purpose. If the number looks big, a human (or a later session) can
    read the real content -- slack_message_log now stores blocks_json too -- and
    decide what's worth muting.

    Attribution is by ticker mention in the message text: slack_message_log has
    no node/ticker column (ts/mode/text/error/blocks_json only), so this is an
    approximation, and a message naming two such tickers counts once. Returns
    None when no sub-threshold node exists at all (nothing to report)."""
    nodes = sub_threshold_real_nodes(threshold)
    tickers = sorted({n['ticker'] for n in nodes if n.get('ticker')})
    if not tickers:
        return None
    with db._conn() as c:
        since = c.execute("SELECT datetime('now', ?)", (f'-{days} day',)).fetchone()[0]
    pattern = re.compile(r'\b(' + '|'.join(re.escape(t) for t in tickers) + r')\b')
    rows = db.get_slack_messages(since=since, limit=100000)
    count = sum(1 for r in rows if pattern.search(r.get('text') or ''))
    return (f"Slack volume: {count} message(s) in {days}d for the "
            f"{len(tickers)} real node(s) under ${threshold:,.0f} — review trigger only")


def incident_lines(check_date):
    """Any trading_incidents row logged on check_date -- real code/process bugs,
    a different thing from coverage_deviations (a daily scenario-expectation
    miss). Returns [] when there are none, so the caller omits the section
    entirely rather than printing a reassuring '0 new bugs' line every night.

    trading_incidents.ts is stored via SQLite's datetime('now') -- UTC -- while
    check_date is an ET trading-calendar date, so the comparison converts
    first. Without it, anything logged after 20:00 ET reads as tomorrow's
    incident (same UTC-vs-local trap documented in coverage_check.run_check's
    added_at guard)."""
    lines = []
    for inc in db.get_incidents(limit=50):
        ts = inc.get('ts') or ''
        with db._conn() as c:
            local_date = c.execute("SELECT date(?, 'localtime')", (ts,)).fetchone()[0]
        if local_date != check_date:
            continue
        where = " ".join(x for x in (inc.get('ticker'), inc.get('account')) if x)
        impact = " [REAL MONEY]" if inc.get('real_money_impact') else ""
        resolved = " (resolved)" if inc.get('resolved_ts') else ""
        head = f"{where} — " if where else ""
        lines.append(f":beetle: New incident today{impact}: {head}{inc['title']}{resolved}")
    return lines


def _unexplained_digest(real, canary):
    n = len(real) + len(canary)
    if not n:
        return "no unexplained"
    if not real:
        return f"{n} unexplained (all canary)"
    if not canary:
        return f"{n} unexplained (all REAL nodes)"
    return f"{n} unexplained ({len(real)} REAL, {len(canary)} canary)"


def digest_line(check_date, money, incidents, real, canary):
    """Line 1: the whole night in one phone-notification-width sentence.

    Replaces `*Coverage Report — <date>*`, which spent the only line Slack's
    mobile preview reliably shows on a title and a date the user already knows,
    while both things they actually check (money, new bugs) sat ~11 lines down
    past the fold.

    `money` is a pre-rendered string, not a number, so a failed price fetch
    degrades to 'P&L unavailable' here instead of taking the digest down with
    it. It is deliberately never labelled 'today' -- see todays_pnl."""
    if incidents is None:
        incident_bit = "incidents unknown"
    elif incidents == 0:
        incident_bit = "no incidents"
    else:
        incident_bit = f"{incidents} incident" + ("s" if incidents != 1 else "")
    return (f"*{check_date[5:]} · {money} · {incident_bit} · "
            f"{_unexplained_digest(real, canary)}*")


def unexplained_block(real, canary):
    """Real-node deviations get one full bullet each and their own red header;
    canary ones collapse to a single counted line.

    The asymmetry is the point (Opus review, 2026-08-14): a canary/test node
    missing its expectation is structurally expected on most nights, so listing
    those bullets undifferentiated alongside a real-money node's deviation
    trains the eye to skip the whole block -- exactly when the one line that
    matters is in it."""
    if not real and not canary:
        return [":white_check_mark: No unexplained deviations."]
    lines = []
    if real:
        lines.append(f":red_circle: {len(real)} UNEXPLAINED on REAL node(s):")
        for r in real:
            lines.append(f"  • {r['scenario_key']} ({r['ticker'] or 'n/a'}): {r['summary']}")
    if canary:
        counts = {}
        for r in canary:
            counts[r['scenario_key']] = counts.get(r['scenario_key'], 0) + 1
        detail = ", ".join(f"{k} x{v}" if v > 1 else k for k, v in sorted(counts.items()))
        lines.append(f":large_yellow_circle: {len(canary)} unexplained on canary/test "
                     f"node(s) (expected most nights): {detail}")
    return lines


def compose(check_date, results, reasons, price_fn=None):
    """The full Coverage Report body, top to bottom:
      1. the digest line -- money, new incidents, unexplained count with a
         canary-vs-real breakdown,
      2. the unexplained-deviation alert, real nodes first and canary collapsed,
      3. the P&L rollup (lifetime-to-date, explicitly not a today-only mark),
      4. any incident logged today (omitted entirely when there are none),
      5. one collapsed rollup line per scenario group + a pointer to the
         standalone tool for the full breakdown,
      6. the weekly Slack-volume review trigger, LAST -- it's a weekly number
         posted daily and neither of the two things the user checks nightly, so
         it sits below everything that is (Opus review, 2026-08-14; it used to
         be wedged between the money line and the incidents line).
    Each piece is individually try/except'd: a price fetch or an incident query
    must never cost the user the unexplained-deviation alert."""
    unexplained = [r for r in results if r['status'] == 'deviated'
                   and r.get('ticket_eligible', True) and not reasons.get(_key(r))]
    try:
        real, canary = split_unexplained(unexplained)
    except Exception:
        # A failed node lookup must not hide a deviation: treat every one as
        # real, which over-reports rather than under-reports.
        real, canary = unexplained, []

    try:
        pnl = todays_pnl(check_date, price_fn=price_fn)
        money = f"{_money(pnl['realized'] + pnl['unrealized'])} realized+open"
        # 2026-08-15 review finding (F2): the digest total silently excluded any
        # unpriced open position -- the detail line below said so, but line 1
        # (the only thing Slack's mobile preview reliably shows) stated a clean
        # total as if every real position had been measured. A total that's
        # missing data must say so at the point it's stated, not three lines down.
        if pnl['open_unpriced']:
            money += f" ({pnl['open_unpriced']} unpriced, excluded)"
        money_detail = pnl_line(check_date, pnl=pnl)
    except Exception as e:
        pnl, money = None, "P&L unavailable"
        money_detail = f"Portfolio: P&L unavailable ({e})"

    try:
        incidents = incident_lines(check_date)
    except Exception as e:
        incidents = [f":beetle: incident check failed ({e})"]
        incident_count = None
    else:
        incident_count = len(incidents)

    lines = [digest_line(check_date, money, incident_count, real, canary)]
    lines.extend(unexplained_block(real, canary))

    lines.append("")
    lines.append(money_detail)
    lines.extend(incidents)

    lines.append("")
    lines.extend(rollup_lines(results, reasons))
    lines.append(f"_Full per-scenario detail: scripts/coverage_check.py --date {check_date}_")

    try:
        volume = slack_volume_line()
    except Exception as e:
        volume = f"Slack volume: unavailable ({e})"
    if volume:
        lines.append(volume)
    return lines
