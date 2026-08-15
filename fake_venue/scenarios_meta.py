"""Scenario constants the entrypoint needs BEFORE any project import.

Kept separate from fake_venue/scenarios.py on purpose: the entrypoint must
know the harness's ticker/account-alias scope in order to build and assert the
isolated environment, and importing the scenario module itself at that point
would be one refactor away from dragging a project module (and its import-time
path reads) in ahead of configure_env(). scenarios.py imports these back, so
there is still exactly one definition of each value.
"""
# The node/order ticker is deliberately SYNTHETIC and unmistakable (repo
# convention: TEST_*_SCENARIO, see the "recognize test fixture tickers" note) --
# a real symbol here would make a stray un-isolated row indistinguishable from
# real activity in a log or DB, which is the whole point of the tripwire list
# below it in isolation.py.
TICKER = "TEST_FAKE_VENUE_SCENARIO"
# ...but the PRICE is still real market data (design decision #7, "market data
# stays real"): the live quote is pulled for this real, liquid symbol and
# seeded onto the synthetic ticker above. Phase 2's replay mode replaces the
# feed, not this indirection.
PRICE_SOURCE_TICKER = "XLK"
CASH_ALIAS = "fv_cash"
MARGIN_ALIAS = "fv_margin"
ALIASES = (CASH_ALIAS, MARGIN_ALIAS)
CASH_ACCOUNT_NUMBER = "88880001"
MARGIN_ACCOUNT_NUMBER = "88880002"
