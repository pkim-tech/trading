#!/bin/bash
# Ordered v4 SL-sweep backfill queue — run this yourself in a terminal, not via
# Claude Code (avoids config.json races with any background agent-launched runs).
# All steps: --max-phase 2.5 (Phase3 confirmed 0/30 win rate this session, see
# docs/backlog_cache.md), entry_timing=open_check only (won 17/17 tested
# campaigns vs close). Logs to console AND logs/backfill_queue_<timestamp>.log
# via tee.
#
# Order (agreed 2026-07-15):
#   1. KORU stop_loss={24,27,30}, open_check only
#   2. SOXL + KORU stop_loss={1,2,4,5}, open_check only (density fill around
#      the already-strong low-SL region)
#   3. Rest of the 11-ticker live watchlist (AGQ,DPST,EDC,GDXU,HIBL,LABU,NUGT,
#      TQQQ,YANG), dense stop_loss grid {1,2,3,4,5,6,9} (capped at 9% 2026-07-16
#      -- see DENSE_SLS comment below), open_check only
#   4. Non-watchlist tickers from the 53-ticker universe whose best v3.x alpha
#      (TrailingBothZScoreBreakout) was >= 500% — computed live at run time,
#      not hardcoded. Same dense grid, open_check only.
#
# Resumable (2026-07-16): before each (ticker, stop_loss, open_check) combo,
# checks scripts/v4_campaign_done.py -- skips it if backtest_cache already has
# Phase2-Island rows for that exact combo (proxy for "already ran under
# --max-phase 2.5"), so a cancelled/restarted queue doesn't redo completed work.
#
# Usage: ./scripts/run_backfill_queue.sh
#   (run from repo root, or anywhere -- it cd's to repo root itself)

set -e
cd "$(dirname "$0")/.."

mkdir -p logs
LOG="logs/backfill_queue_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to $LOG (console + file via tee)"

export MAX_PHASE=2.5
DENSE_SLS="1 2 3 4 5 6 9"
# Capped at 9% (2026-07-16) -- see scripts/run_v4_backfill_sweep.sh's matching
# comment; robust_alpha declines consistently above this point, not worth the
# compute. GDXU's in-progress run (paused at stop_loss=18) will stop advancing
# past 9 going forward -- already-completed 12+ rows for GDXU/EDC/SOXL/KORU are
# left in place, just not extended to the rest of the watchlist.
LOW_SLS="1 2 4 5"
WATCHLIST="AGQ DPST EDC GDXU HIBL KORU LABU NUGT SOXL TQQQ YANG"
REST_OF_WATCHLIST="AGQ DPST EDC GDXU HIBL LABU NUGT TQQQ YANG"  # minus SOXL/KORU

run_if_needed() {
  local sl=$1 ticker=$2
  if .venv/bin/python scripts/v4_campaign_done.py "$ticker" "$sl" open_check; then
    echo "  [skip] $ticker stop_loss=$sl open_check already done"
    return
  fi
  ./scripts/run_v4_backfill_sweep.sh "$sl" open_check "$ticker" --skip-cache-refresh
}

{
  echo "======================================================"
  echo " Backfill queue start — $(date)"
  echo "======================================================"

  # ── Step 1: KORU stop_loss 24,27,30, open_check only ──────────────────────
  echo ""
  echo "### STEP 1: KORU stop_loss={24,27,30} open_check ###"
  for sl in 24 27 30; do
    run_if_needed "$sl" KORU
  done

  # ── Step 2: SOXL + KORU stop_loss 1,2,4,5, open_check only ────────────────
  echo ""
  echo "### STEP 2: SOXL+KORU stop_loss={1,2,4,5} open_check ###"
  for ticker in SOXL KORU; do
    for sl in $LOW_SLS; do
      run_if_needed "$sl" "$ticker"
    done
  done

  # ── Step 3: rest of the watchlist, full dense grid, open_check only ───────
  echo ""
  echo "### STEP 3: rest of watchlist ($REST_OF_WATCHLIST) — dense SL grid, open_check ###"
  for ticker in $REST_OF_WATCHLIST; do
    for sl in $DENSE_SLS; do
      run_if_needed "$sl" "$ticker"
    done
  done

  # ── Step 4: non-watchlist tickers with best v3.x alpha >= 300 ─────────────
  # Lowered from 500 (2026-07-16) -- only GDXD cleared 500, and GDXD's data
  # turned out to be corrupted (uncleaned historical reverse-split drift,
  # $9990->$51 over 3 years, same failure mode as the KORU incident but
  # gradual instead of a single jump -- its v4 numbers should not be trusted).
  echo ""
  echo "### STEP 4: screening non-watchlist tickers for v3 alpha >= 300 ###"
  EXTRA_TICKERS_LIST=$(.venv/bin/python -c "
import sqlite3, sys
conn = sqlite3.connect('cache/research/trading_universe.db')
c = conn.cursor()
watchlist = set('$WATCHLIST'.split())
c.execute('''
    SELECT ticker, MAX(alpha_vs_spy) AS best_alpha
    FROM backtest_cache
    WHERE version LIKE 'v3.%' AND strategy = 'TrailingBothZScoreBreakout' AND trades > 0
    GROUP BY ticker
    HAVING best_alpha >= 300
    ORDER BY best_alpha DESC
''')
rows = [r for r in c.fetchall() if r[0] not in watchlist]
for t, a in rows:
    print(f'  {t}: best v3 alpha {a:+.1f}%', file=sys.stderr)
print(' '.join(r[0] for r in rows))
")

  if [ -z "$EXTRA_TICKERS_LIST" ]; then
    echo "No non-watchlist tickers cleared the 300% v3 alpha bar -- skipping Step 4."
  else
    echo "Step 4 tickers: $EXTRA_TICKERS_LIST"
    for ticker in $EXTRA_TICKERS_LIST; do
      for sl in $DENSE_SLS; do
        run_if_needed "$sl" "$ticker"
      done
    done
  fi

  echo ""
  echo "======================================================"
  echo " Backfill queue complete — $(date)"
  echo "======================================================"
} 2>&1 | tee "$LOG"
