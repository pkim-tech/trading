# Roadmap

Phased priority order for the project, above the level of individual backlog
items -- recorded 2026-08-07 per the user's request for a place to hang
"where are we headed and in what order" so `docs/backlog_cache.md`'s flat
list of open items can be triaged against something, not just worked
first-in-first-out. This is explicitly expected to change as research
findings land -- update it in place rather than treating it as fixed. Not a
replacement for `docs/backlog_cache.md` (open items), `docs/deep_backlog.md`
(permanent archive), or `docs/research_log.md` (experiment history) -- this
is the ordering layer above all three.

## Current phase -- universe sweep for overlay-friendly core nodes

Sweep the broader (liquidity-screened) ticker universe, not to find the best
core strategy alone, but to find core nodes that pair well with the
drought/add-on overlay -- a core node with somewhat lower standalone
performance can still be a good candidate if the overlay "somewhat
multiplies" its return. Output of this phase: a candidate list of
core-node + overlay combinations worth allocating extra capital to. Limited
by real research compute right now (single machine) -- more hardware
(additional local machines, a home VM environment, or renting cloud compute)
is a live open question, not yet decided.

## Next -- portfolio construction

Once the candidate list from the sweep exists: work out how to actually
combine/size multiple core+overlay nodes into a portfolio (capital
allocation across nodes, not just picking each node's own best config in
isolation).

## Then -- go live with modest capital

First real deployment of the portfolio-constructed set, intentionally
modest-sized, not the eventual target scale.

## Parallel/ops -- Slack cleanup

Current Slack alerting is "somewhat useless right now" -- too much
information density for a small screen. Needs a cleanup pass; not gated on
the research phases above, can happen alongside.

## While live -- trend/edge-decay detection (the next big research item)

Core premise: strategy returns are not expected to stay persistent forever
-- old winners will lose their edge and new winners will show up over time,
the same way a ticker's most recent ~3 months can trade meaningfully
differently than its longer history would suggest. Need a real way to
detect this shift (possibly pure momentum/regime-change detection, not
necessarily true predictive forecasting -- forecasting the future is hard,
detecting a recent regime change may be tractable). The practical output:
gives each live ticker/node a lifecycle within the portfolio (when to add,
when to retire) instead of treating "once validated, always validated" as
permanent.

## ~2027 -- scale up

Once trend/edge-decay detection exists alongside the live modest-capital
portfolio: this is the point to meaningfully increase capital deployed
("hit the accelerator"). Any idle time before then can go toward continued
strategy tweaking/new-strategy research, but that work is explicitly
secondary to the phases above, not the main thread.

## How to use this against the backlog

When triaging `docs/backlog_cache.md`, an item that clearly serves the
*current* phase above should generally outrank one that only serves a later
phase, all else equal -- but this is a judgment aid, not a strict gate; a
cheap high-value item from a later phase is still worth doing opportunistically.
