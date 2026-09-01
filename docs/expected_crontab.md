# Expected crontab

Source of truth for what should be in `crontab -l` on this machine. Check drift with
`scripts/check_cron_drift.py` (diffs live `crontab -l` against this file's own table --
parses the fenced block below, not a hand-maintained separate list).

Update this file in the same commit as any `crontab -e` change (add/remove/edit a line).

```cron
0 * * * * cp /home/pkim/git/trading/cache/live/trading_live.db /home/pkim/git/trading/output/live_backups/trading_live_$(date +\%Y\%m\%d_\%H).db.bak && ls -t /home/pkim/git/trading/output/live_backups/*.db.bak | tail -n +721 | xargs rm -f
5 * * * * cp /home/pkim/git/trading/cache/live/trading_live.db /mnt/c/Users/pjkim/Documents/trading_backups/trading_live_$(date +\%Y\%m\%d_\%H).db.bak && ls -t /mnt/c/Users/pjkim/Documents/trading_backups/*.db.bak | tail -n +721 | xargs rm -f
0 2 * * * cp /home/pkim/git/trading/cache/research/trading_universe.db /home/pkim/git/trading/cache/research/trading_universe_daily.db.bak
30 6 * * * /home/pkim/git/trading/scripts/run_data_collector.sh >> /home/pkim/git/trading/logs/data_collector_daily.log 2>&1
15 4 * * * /home/pkim/git/trading/.venv/bin/python3 /home/pkim/git/trading/db_cache.py >> /home/pkim/git/trading/logs/db_cache_daily.log 2>&1
0 10 * * 1-5 /home/pkim/git/trading/.venv/bin/python3 /home/pkim/git/trading/scripts/collect_options_snapshot.py --tickers AGQ DFEN DPST HIBL JNUG KORU LABU NUGT SOXL UGL WEBL --option-type calls --expirations 2 >> /home/pkim/git/trading/logs/options_snapshot_daily.log 2>&1
45 3 * * * /home/pkim/git/trading/.venv/bin/python3 /home/pkim/git/trading/scripts/add_usage_tracking.py >> /home/pkim/git/trading/logs/script_usage_tracking_nightly.log 2>&1 && /home/pkim/git/trading/.venv/bin/python3 /home/pkim/git/trading/scripts/check_script_usage_convention.py >> /home/pkim/git/trading/logs/script_usage_tracking_nightly.log 2>&1
*/15 * * * * /home/pkim/git/trading/.venv/bin/python3 /home/pkim/git/trading/scripts/corp_action_canary.py >> /home/pkim/git/trading/logs/corp_action_canary.log 2>&1
3 16 * * 1-5 cd /home/pkim/git/trading && .venv/bin/python3 scripts/refresh_recent_minute_cache.py >> /home/pkim/git/trading/logs/refresh_recent_minute_cache.log 2>&1
```

Captured 2026-09-01 from the real `crontab -l` output, after the `refresh_recent_minute_cache.py`
line (Task #11) was added.
