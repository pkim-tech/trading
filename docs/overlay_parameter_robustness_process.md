# Overlay/Config-Parameter Robustness Validation Process

Distinct from `docs/watchlist_candidate_checklist.md` (validates a ticker's *core* strategy
parameters before promotion) — this is for validating a *new overlay parameter choice* on top of an
already-frozen core config (drought-overlay confirm_days/vol_gate, or any future overlay's own
tunable knobs). Built 2026-08-07 out of the SOXL drought-overlay and add-on validation work.

## What "an overlay" actually is (the taxonomy, for future ideas)

The real question every overlay in this category answers is: **"we already have a great starting
result — how do we make it better, safely, without touching the part that already works?"** Not a
search for a brand-new independent edge — an augment on top of an already-validated core. Every
overlay explored this session is the same shape:
- **Avoid losses** on an existing position (put-hedge — caps downside on a position core already
  holds). Has its own real parameter to sweep/validate (OTM strike %).
- **Use otherwise-idle capital during a gap** (drought overlay — core is flat, no signal, deploy the
  same risk mechanics on a reactive time+vol confirmation instead of leaving capital doing nothing).
  Has its own real parameters to sweep/validate (confirm_days, vol_gate) — this is why it needed the
  long search this session, unlike add-on below.
- **Double down when the signal is usually positive** (add-on — core's own trailing-arm condition is
  a real, already-validated signal that the position is working; add-on borrows more of the same bet
  at that confirmed-good moment). **Has no independent parameter to sweep at all** — it inherits
  core's own trigger and sizing entirely (100%-of-position margin, always), so validating it is just
  the out-of-sample/stress-test steps below directly, no separate parameter search first. This is
  the real reason add-on validated so much faster and more cleanly than drought overlay did.

Any future overlay idea should be checked against this taxonomy first: which of the three is it
actually doing, and does that framing make the idea's real risk/reward shape clear before any
backtesting starts? An idea that doesn't fit any of the three cleanly may be a different kind of
thing entirely (a new core strategy, not an overlay) and shouldn't be forced into this validation
process.

**Read the honest framing below before treating this as a mechanical recipe** — the actual finding
this session (SOXL's real confirm_days=3/vol_gate=0.4 signal) wasn't produced by grinding through
these steps in order. It came from noticing something that looked odd (a suspicious concentration
of results in one time period, a parameter that kept winning for a structural rather than causal
reason) and *not* accepting the first discouraging-looking pass as final — asking "why," pushing
past a couple of points where the analysis was heading toward "reject and move on," and only then
running the mechanical steps below to confirm or refute what the curiosity turned up. The steps are
real and worth applying every time; they are not a substitute for noticing when something looks
worth a second look, and they should not be used to prematurely close off a thread just because an
early pass looked unpromising.

## The mechanical steps (necessary, not sufficient on their own)

1. **Search only on fit-half data.** Split the available history chronologically (roughly 50/50 is
   fine). Run whatever grid/scan you're using (day-by-day confirm_days, vol_gate sweep, etc.) using
   *only* the fit half — the test half must never influence which candidate gets selected.

2. **Only check the test half for candidates that already clear a real bar on fit.** Don't run the
   test-half check on every candidate indiscriminately — decide the bar first (this session used "the
   user's actual return target," not an arbitrary statistical threshold), and only spend the
   out-of-sample check on things that already look real.

3. **Stress-test by removing the single biggest winner and rechecking.** If the result flips from
   clearly positive to clearly negative (or vice versa) when one trade is removed, the "edge" is a
   single-trade artifact, not real — reject regardless of what the fit/test split showed. This step
   alone caught KORU's fake "pass" in the 18-ticker batch scan that the fit/test split alone had
   missed.

4. **Confirm real differential selection via an included-vs-excluded split.** For a filter (like a
   vol-gate), don't just look at what got kept — look at what got *excluded* and check whether it's
   meaningfully worse. If excluded and included trades look similarly good/bad, the filter isn't
   doing real selective work, even if the included set alone looks positive.

5. **When something surprising survives 1-4, ask why before trusting it.** SOXL's real signal
   surviving didn't end the investigation — checking *why* (real chop within an exceptional rally
   period, not a smooth ride; the specific parameter's neighbor sensitivity; whether the underlying
   mechanism made economic sense) is what turned "passed four checks" into an actual, defensible
   understanding worth acting on.

## What actually made this work this session

Re-reading how the SOXL finding actually emerged: several points in the conversation were heading
toward "this doesn't work, drop it" (the plain default failed cliff-safety; several tuned/gated
variants failed the fit/test split) before a specific instinct — noticing that shorter confirm_days
values were being systematically undervalued because they hadn't been checked, not because they'd
been checked and rejected — reopened the thread and led to the real result. **The mechanical checklist
above is what makes a finding trustworthy once you have one; it is not what generates the finding.**
Don't skip step 5's "why" question, and don't treat an initial negative pass through steps 1-4 as
proof there's nothing there if a real anomaly or unchecked region hasn't actually been looked at yet.
