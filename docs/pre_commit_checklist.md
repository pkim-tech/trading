# Pre-Commit Checklist

Used by `feature wrap` and `session wrap` before committing.

## Checks

- [ ] No secrets or API keys in staged files (check `.env` is gitignored)
- [ ] No runtime artifacts staged (`cache/`, `logs/`, `output/`, `active_phase_grid.json`, `current_test.json`)
- [ ] `docs/design.md` reflects any architectural changes made this session
- [ ] `docs/backlog_cache.md`/`docs/deep_backlog.md` updated if new issues or ideas surfaced
- [ ] `readme.md` updated if layer behavior changed
- [ ] Staged files reviewed — nothing unexpected included

## Notes
- This list grows over time as real mistakes are caught — add to it when something slips through
