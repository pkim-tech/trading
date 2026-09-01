---
name: long-job-launch
description: Checklist for launching any script/command expected to run more than ~a minute (sweep queues, full-checklist reports, DB prunes, batch backtests) so progress is actually checkable mid-run instead of silent until it exits. Use before backgrounding any long-running Bash command or before writing a script that will be run that way.
---

# Long job launch

Built 2026-08-24 after the same mistake hit two separate sessions: a background
job gave zero visibility into progress for 35+ minutes because its own stdout was
piped through `tail -N` (which buffers everything until the process exits — not a
live `tail -f`), and the script itself had no index/total or ETA output. The user's
real complaint: "i have just no idea when to come back."

## When to use

- About to background any Bash command likely to run more than ~a minute —
  sweep queues, full-checklist candidate reports, DB prune/validate/swap passes,
  batch backtests, anything iterating a known list of tickers/scopes.
- Writing or editing a script that will typically be launched this way.

## The checklist, every launch

1. **Never pipe the command's stdout through `tail`/`head`.** `... | tail -40`
   buffers until EOF, so nothing appears until the process is already done —
   defeating the entire point of backgrounding it to check on later. Redirect
   straight to a real log file instead: `command > logfile.log 2>&1`.

2. **Prefer `run_in_background: true` on the Bash tool call itself** over manual
   `nohup ... &` + a separate polling loop — it gives a task id you can `Read`
   or get notified on, without an extra wrapper process.

3. **If the script's total unit count is knowable upfront** (a fixed list of
   tickers/scopes/tranches), and you're touching the script anyway, add an
   `[i/total, Xs elapsed, ETA Ys]` line per unit rather than a bare status
   line — resolve the full list before the slow work starts so the total is
   known from the first line printed. A simple `elapsed / done * remaining`
   projection is enough; it doesn't need to be a real estimator.
   Example (from `scripts/build_v6_promotion_combined_report.py`'s
   `build_rows()`): resolve `plan = [(ticker, strategy, version, fixed_sl), ...]`
   first, then `for i, unit in enumerate(plan, start=1): ...`.

4. **Tell the user how to check on it themselves**, not just "I'll let you
   know" — the log file path, and that `tail -f <path>` shows it live if they
   want to look before the notification arrives.

5. **Don't `wsl --shutdown` / kill / restart anything while a long job you
   started is still running** — check `ps aux` for the script name first if
   there's any doubt.

6. **Before launching, ask whether the work is actually independent and can run
   in parallel instead of serially.** Found 2026-08-24 (same session as the
   1m-vs-1s SOXL walk): running two single-threaded per-ticker/per-config
   passes back-to-back left half the machine's cores idle for no reason — check
   `nproc` (or just note the job is single-threaded Python/CPU-bound with no
   shared mutable state) and launch each independent unit as its own
   `run_in_background` call instead of chaining them in one script/loop. Rule
   of thumb: independent if they read different input files (or the same file
   read-only) and write to different output files — don't parallelize writers
   sharing one output file/DB table without checking for a real write-lock
   conflict first. This compounds with item 3 above: a script whose per-unit
   loop is already sequential (a fetch queue, a batch-adjustment pass) can
   usually be split into N parallel invocations over disjoint slices of the
   same unit list, same pattern as launching N independent jobs — this was
   also the fix that turned an 11-ticker sequential dividend-adjustment pass
   into 4 parallel ones tonight.

7. **Cap parallel launches to real headroom, not just "how many independent
   units exist."** Found 2026-08-25: 14 independent CPU-bound per-ticker jobs
   were launched at once on a 12-core box, pushing load average to 17+ and
   slowing the real live trading daemon (`active_signals.py run`, which shares
   this machine and must stay responsive) — on a real trading day, not
   off-hours. **The full US market session, 9:00 AM-4:00 PM ET, is the
   responsiveness-critical window** (not just the two 10:25-10:40/15:25-15:40
   ET signal-check sub-windows) — the daemon's poll loop, order placement,
   and fill/exit monitoring all need to stay responsive across the whole
   session, not only during signal checks. Before firing N parallel
   single-threaded jobs during 9-4 ET on a trading day: check `nproc` and
   `uptime` (or `ps aux --sort=-%cpu` for what's already running, including
   the daemon), and if N exceeds cores-minus-headroom, split into tranches
   (e.g. batches of ~half the core count) launched one after another instead
   of all at once. This is tighter than the general
   `feedback_scope_jobs_to_reset_window` guidance — it's about concurrent CPU
   contention with a live process, not usage-quota pacing. Whether it's a
   trading day/trading-hours window matters here specifically because the
   daemon's responsiveness requirement is real then; off-hours the same batch
   is much lower-stakes (see CLAUDE.md's Background-Agent Trading-Hours Rule
   for the parallel concern about spawning agents, not just raw CPU jobs).

8. **For a sweep/campaign queue covering a known ticker/strategy list, verify
   the queued jobs actually match intent before walking away** -- don't just
   trust that the launch command's ticker list was complete. Found 2026-09-01:
   a real campaign (`campaign_jobs`, "v6.5") was assembled from several
   separate `TICKERS=...` launches and silently ended up missing a ticker
   entirely (DFEN) plus one ticker's real live strategy (DPST's TrailingExit)
   -- nobody checked until asked directly, a full session+ into the run.
   `scripts/check_campaign_coverage.py --campaign-id N` diffs a campaign's
   real queued (ticker, strategy) pairs against the real capital-at-stake live
   ticker set (both strategies each, by default) and reports gaps -- run it
   right after queuing, not after the run finishes.
