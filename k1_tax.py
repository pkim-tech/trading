"""K-1 / Section 1256 trading tax forecast tool -- planning aid only, not a K-1 parser
and not a substitute for CPA-prepared numbers. Every computed output here is
indicative until the actual K-1 arrives (typically Feb-Mar of the following year).

Core mechanics modeled (see docs/research_log.md and conversation history for the
full writeup of why these apply to AGQ specifically):

- Section 1256 60/40 treatment: 60% of a PTP's ANNUAL net gain is taxed at the
  long-term rate, 40% at the short-term/ordinary rate, regardless of actual holding
  period. The tax event is the calendar year, not the individual trade.
- PTP passive-loss silo (IRC 469(k)): a loss in one PTP can only offset income from
  that SAME PTP, never another PTP or other income. Every calc here is scoped
  per-PTP; nothing nets across PTPs.
- IRS 110% prior-year safe harbor: quarterly estimated payments are sized off last
  year's actual total tax liability, not this year's in-progress gains -- this
  year's trading only changes NEXT year's required payment, once this year's return
  is filed.

Two separate reserve buckets, deliberately not conflated:
  Bucket A -- tax due at filing (~Apr 15 of the following year) on this year's net gain.
  Bucket B -- the incremental step-up to next year's required quarterly safe-harbor
              payment, NOT a second full reservation of the same gain.
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "cache" / "research" / "k1_tax.db")

SAFE_HARBOR_MULTIPLIER = 1.10  # IRS 110% prior-year safe harbor (applies above the AGI threshold)
QUARTERLY_DUE_MONTH_DAY = [(4, 15), (6, 15), (9, 15), (1, 15)]  # Q1..Q4, Q4 due next calendar year


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RateConfig:
    """All rate inputs are configurable, not hardcoded -- actual bracket varies with
    total income and changes year to year."""
    federal_ordinary_rate: float = 0.37
    federal_lt_rate: float = 0.20
    niit_rate: float = 0.038
    state_rate: float = 0.109   # NY State top bracket
    city_rate: float = 0.03876  # NYC top bracket
    section_1256_lt_fraction: float = 0.60  # 60/40 split; configurable for a non-1256 K-1
    section_1256_st_fraction: float = 0.40

    def blended_rate(self) -> float:
        """Effective rate applied to a Section-1256-eligible PTP's annual net gain.
        LT portion pays fed-LT + NIIT + state + city; ST portion pays fed-ordinary +
        NIIT + state + city (NIIT/state/city don't distinguish LT/ST)."""
        lt_rate = self.federal_lt_rate + self.niit_rate + self.state_rate + self.city_rate
        st_rate = self.federal_ordinary_rate + self.niit_rate + self.state_rate + self.city_rate
        return (self.section_1256_lt_fraction * lt_rate
                + self.section_1256_st_fraction * st_rate)


@dataclass
class Trade:
    trade_date: date
    ptp: str
    gain: float  # signed: positive = gain, negative = loss
    note: str = ""


@dataclass
class BucketStatus:
    needed: float
    reserved: float

    @property
    def shortfall(self) -> float:
        return max(0.0, self.needed - self.reserved)

    @property
    def fully_reserved(self) -> bool:
        return self.reserved >= self.needed - 1e-9


@dataclass
class QuarterlyRequirement:
    label: str          # e.g. "Q3 2026"
    due_date: date
    required_amount: float
    cumulative_required: float
    cumulative_paid: float

    @property
    def behind(self) -> bool:
        return self.cumulative_paid < self.cumulative_required - 1e-9

    @property
    def behind_by(self) -> float:
        return max(0.0, self.cumulative_required - self.cumulative_paid)


# ---------------------------------------------------------------------------
# Annual netting (per-PTP silo -- never net across PTPs)
# ---------------------------------------------------------------------------

def annual_net_gain_by_ptp(trades: list[Trade], year: int) -> dict[str, float]:
    """Running annual total per PTP for the given calendar year. Each PTP's own
    K-1 nets its own trades into one annual figure -- this is the number the 60/40
    split and reserve calcs apply to, not any individual trade."""
    totals: dict[str, float] = {}
    for t in trades:
        if t.trade_date.year != year:
            continue
        totals[t.ptp] = totals.get(t.ptp, 0.0) + t.gain
    return totals


# ---------------------------------------------------------------------------
# Bucket A -- tax due at filing on the current year's net gain
# ---------------------------------------------------------------------------

def bucket_a_tax_due(net_gain: float, rates: RateConfig) -> float:
    """Blended rate x net gain. Only meaningful for a net GAIN -- a net loss reduces
    a future filing's liability but isn't refundable cash today, so this returns 0
    for a net loss (the loss itself is tracked separately, see suspended_loss below)."""
    if net_gain <= 0:
        return 0.0
    return net_gain * rates.blended_rate()


def suspended_loss_carryforward(net_gain: float) -> float:
    """PTP passive-loss silo (469(k)): a net loss in a PTP just carries forward
    against that SAME PTP's future income -- not usable elsewhere. Returns the
    positive carryforward amount, or 0 if this year was a net gain."""
    return max(0.0, -net_gain)


# ---------------------------------------------------------------------------
# Bucket B -- incremental safe-harbor step-up (NOT a second full reservation)
# ---------------------------------------------------------------------------

def bucket_b_step_up(prior_year_liability: float, updated_current_year_liability: float,
                      buffer_quarters: int = 4) -> float:
    """The incremental increase to NEXT year's required quarterly estimate once this
    year's return is filed: 110% x (this year's total liability - last year's total
    liability), covering `buffer_quarters` of that step-up (default: all 4, i.e. the
    full annual step-up). `buffer_quarters` lets the user dial down to e.g. 2 quarters
    of buffer, or up via a caller-supplied multiplier for "cover 100% of one year's
    gain" (pass updated_current_year_liability = prior + net_gain*blended_rate and
    buffer_quarters=4 for the fully-conservative option -- this function doesn't
    special-case that, callers compose it).
    Deliberately does NOT default to "reserve the whole gain again": this is only the
    delta in the *required quarterly payment*, not Bucket A's full tax-due amount.
    """
    required_next_year = SAFE_HARBOR_MULTIPLIER * updated_current_year_liability
    required_this_year = SAFE_HARBOR_MULTIPLIER * prior_year_liability
    step_up_annual = max(0.0, required_next_year - required_this_year)
    return step_up_annual * (buffer_quarters / 4.0)


# ---------------------------------------------------------------------------
# Safe harbor / quarterly tracker
# ---------------------------------------------------------------------------

def quarterly_due_dates(tax_year: int) -> list[date]:
    """Q1-Q3 due within tax_year; Q4 due Jan 15 of the following year."""
    dates = []
    for i, (month, day) in enumerate(QUARTERLY_DUE_MONTH_DAY):
        yr = tax_year if i < 3 else tax_year + 1
        dates.append(date(yr, month, day))
    return dates


def safe_harbor_schedule(tax_year: int, prior_year_liability: float,
                          payments_made: list[tuple[date, float]] | None = None
                          ) -> list[QuarterlyRequirement]:
    """Required annual payment = 110% of prior year's actual total tax liability,
    split into 4 equal quarterly amounts. `payments_made` is a list of (date, amount)
    actual payments; cumulative_paid at each due date only counts payments made on
    or before that due date, so a quarter can be flagged behind even if a later
    payment would eventually cover it (matches the IRS's per-period underpayment
    penalty logic, which is date-sensitive, not just an end-of-year total)."""
    payments_made = payments_made or []
    required_annual = SAFE_HARBOR_MULTIPLIER * prior_year_liability
    required_per_quarter = required_annual / 4.0
    due_dates = quarterly_due_dates(tax_year)

    schedule = []
    cumulative_required = 0.0
    for i, due in enumerate(due_dates):
        cumulative_required += required_per_quarter
        cumulative_paid = sum(amt for d, amt in payments_made if d <= due)
        schedule.append(QuarterlyRequirement(
            label=f"Q{i + 1} {tax_year}",
            due_date=due,
            required_amount=required_per_quarter,
            cumulative_required=cumulative_required,
            cumulative_paid=cumulative_paid,
        ))
    return schedule


def next_due(schedule: list[QuarterlyRequirement], as_of: date | None = None) -> QuarterlyRequirement | None:
    as_of = as_of or date.today()
    upcoming = [q for q in schedule if q.due_date >= as_of]
    return min(upcoming, key=lambda q: q.due_date) if upcoming else None


# ---------------------------------------------------------------------------
# Stress test / scenario toggle
# ---------------------------------------------------------------------------

def stress_test_flat_quarter(reserved_b: float, required_quarterly: float,
                              trading_delta_pct: float = 0.0) -> dict:
    """Simulates 'next quarter's trading is flat / down X%' against the FIXED
    required quarterly payment (safe harbor is prior-year based, so it does not move
    with this scenario) -- shows whether Bucket B's existing buffer would cover the
    due payment without touching principal/trading capital. trading_delta_pct is
    informational context only (e.g. -20% down quarter) since it doesn't change the
    fixed required_quarterly figure; included so a caller can label the scenario."""
    shortfall = max(0.0, required_quarterly - reserved_b)
    return {
        "trading_delta_pct": trading_delta_pct,
        "required_quarterly": required_quarterly,
        "reserved_b": reserved_b,
        "shortfall": shortfall,
        "covered_without_touching_capital": shortfall <= 1e-9,
    }


# ---------------------------------------------------------------------------
# Brokerage-only realized-loss-baseline netting + reserve recommendation
# (docs/deep_backlog.md's 2026-08-15 tax-forecast model -- distinct from the
# Bucket A/B safe-harbor machinery above, which assumes every logged Trade is
# already a Section-1256 PTP gain. This piece nets a real dollar realized-loss
# baseline against a MIX of Section-1256 (60/40 split) and ordinary
# short-term tickers, scoped to `brokerage`, the only taxable account.)
#
# CALIBRATION FRAMING: this is a reserve-SIZING estimator, not a
# filing-accuracy tool. Expected error ~5-10%, not precision-to-the-dollar --
# "we're estimating, the CPA will tell the user if they're short" (user's
# explicit framing, 2026-08-15). Don't chase exact wash-sale-lot-level
# precision here; per-ticker annual net realized $ is the right grain.
# ---------------------------------------------------------------------------

# Real, documented fact (not an assumption made here): AGQ is the one
# Section-1256 futures-linked K-1 PTP trading in `brokerage` today, so its
# annual net gain/loss splits 60% long-term / 40% short-term for character
# purposes regardless of actual holding period. JNUG/ETHU are standard
# '40-Act ETFs -- their entire net gain/loss is ordinary short-term. If a
# 4th Section-1256 ticker is ever added to `brokerage`, add it here.
SECTION_1256_TICKERS = frozenset({"AGQ"})


@dataclass
class BrokerageTaxForecast:
    year: int
    section_1256_gain: dict[str, float]      # {ticker: net $ gain/loss}, Section 1256 tickers only
    ordinary_st_gain: dict[str, float]       # {ticker: net $ gain/loss}, ordinary short-term tickers
    lt_gain_gross: float                     # AGQ's 60% long-term slice, pre-baseline-netting
    st_pool_gross: float                     # JNUG/ETHU gains + AGQ's 40% short-term slice, pre-netting
    st_baseline_loss: float                  # realized-loss baseline, short_term character
    lt_baseline_loss: float                  # realized-loss baseline, long_term character
    lt_gain_net: float                       # lt_gain_gross - lt_baseline_loss, floored at 0
    st_pool_net: float                       # st_pool_gross - st_baseline_loss, floored at 0
    st_baseline_remaining: float             # unexhausted portion of the short-term baseline
    lt_baseline_remaining: float             # unexhausted portion of the long-term baseline
    liability: float                         # Liability = Profit x effective_rate, post-netting
    estimate_already_paid: float
    reserve: float                           # Liability - estimate_already_paid, floored at 0
    baseline_exhausted: bool                 # True once st_baseline AND lt_baseline are both used up
    recommend_full_sweep: bool               # baseline_exhausted AND liability > 0
    note: str


def brokerage_tax_forecast(year: int, realized_gains_by_ticker: dict[str, float],
                            baseline: dict[str, float], rates: RateConfig | None = None,
                            estimate_already_paid: float = 0.0) -> BrokerageTaxForecast:
    """Nets `brokerage`'s real per-ticker realized $ gain/loss for `year` against
    the realized-loss baseline, then computes Liability/Reserve.

    realized_gains_by_ticker: {ticker: net $ gain/loss}, e.g. from
      signals_db.get_realized_pnl_by_ticker('brokerage', [...], year). Tickers
      in SECTION_1256_TICKERS get the 60/40 split; everything else is treated
      as 100% ordinary short-term.
    baseline: {'short_term': amount, 'long_term': amount} -- positive $ amounts
      representing a LOSS baseline, e.g. from
      signals_db.get_tax_realized_loss_baseline('brokerage', year). A missing
      key is treated as 0.
    rates: reuses k1_tax's own RateConfig/blended-rate engine -- pass None to
      load the persisted config via load_rate_config().

    Netting mechanics (the whole point of this function, see module-level
    comment above): the short-term baseline nets against the COMBINED
    short-term pool -- ordinary-ST-ticker gains + the 40%-short-term slice of
    every Section-1256 ticker's gain -- together, not against either side
    alone. The Section-1256 60%-long-term slice is only reduced by a
    long-term-character baseline (0 today, since the real baseline is 100%
    short-term). A negative net per-ticker figure (a real realized loss this
    year) already reduces its own pool correctly since it's a signed sum --
    no separate "this year's own losses" handling is needed beyond summing
    signed per-ticker gains into the two pools.
    """
    rates = rates or load_rate_config()

    section_1256_gain = {t: g for t, g in realized_gains_by_ticker.items() if t in SECTION_1256_TICKERS}
    ordinary_st_gain = {t: g for t, g in realized_gains_by_ticker.items() if t not in SECTION_1256_TICKERS}

    lt_gain_gross = sum(g * rates.section_1256_lt_fraction for g in section_1256_gain.values())
    st_pool_gross = (sum(g * rates.section_1256_st_fraction for g in section_1256_gain.values())
                      + sum(ordinary_st_gain.values()))

    st_baseline_loss = baseline.get("short_term", 0.0)
    lt_baseline_loss = baseline.get("long_term", 0.0)

    # Cross-character netting (found in review, 2026-08-15): real tax mechanics
    # let an excess loss of one character offset a gain of the other -- this
    # applies to THIS YEAR'S OWN trading result (lt_gain_gross/st_pool_gross,
    # already signed sums so either can be a real net loss on its own), never
    # to the baseline crossing characters (the baseline is a fixed prior-year
    # carryforward, same-character-only by design -- test_long_term_baseline_
    # only_offsets_long_term_slice_not_short_term_pool pins that). Independent
    # per-character (gross - baseline) flooring overstated liability whenever
    # one character was a net loss this year while the other was a net gain
    # (e.g. AGQ's LT slice +$100k while JNUG/ETHU net -$60k ST: real netted
    # position ~$40k taxable, the old code taxed the full $100k and reported
    # the loss side as exactly 0 instead of contributing its excess).
    lt_net = lt_gain_gross
    st_net = st_pool_gross
    if lt_net < 0 and st_net > 0:
        st_net += lt_net
        lt_net = 0.0
    elif st_net < 0 and lt_net > 0:
        lt_net += st_net
        st_net = 0.0
    lt_gain_net = max(0.0, lt_net - lt_baseline_loss)
    st_pool_net = max(0.0, st_net - st_baseline_loss)
    # baseline_remaining: how much of the ORIGINAL baseline principal is still
    # unused. Keyed off the (already cross-netted, so never double-counts a
    # loss that already offset the other character) per-character result,
    # floored at 0 before subtracting from the baseline -- a real trading LOSS
    # this year doesn't inflate how much baseline is left beyond its own
    # principal (the old code could report MORE remaining baseline than
    # actually exists whenever gross was negative).
    lt_baseline_remaining = max(0.0, lt_baseline_loss - max(0.0, lt_net))
    st_baseline_remaining = max(0.0, st_baseline_loss - max(0.0, st_net))

    lt_rate = rates.federal_lt_rate + rates.niit_rate + rates.state_rate + rates.city_rate
    st_rate = rates.federal_ordinary_rate + rates.niit_rate + rates.state_rate + rates.city_rate
    liability = lt_gain_net * lt_rate + st_pool_net * st_rate

    reserve = max(0.0, liability - estimate_already_paid)

    baseline_exhausted = st_baseline_remaining <= 1e-9 and lt_baseline_remaining <= 1e-9
    recommend_full_sweep = baseline_exhausted and liability > 1e-9

    return BrokerageTaxForecast(
        year=year, section_1256_gain=section_1256_gain, ordinary_st_gain=ordinary_st_gain,
        lt_gain_gross=lt_gain_gross, st_pool_gross=st_pool_gross,
        st_baseline_loss=st_baseline_loss, lt_baseline_loss=lt_baseline_loss,
        lt_gain_net=lt_gain_net, st_pool_net=st_pool_net,
        st_baseline_remaining=st_baseline_remaining, lt_baseline_remaining=lt_baseline_remaining,
        liability=liability, estimate_already_paid=estimate_already_paid, reserve=reserve,
        baseline_exhausted=baseline_exhausted, recommend_full_sweep=recommend_full_sweep,
        note="ESTIMATOR ONLY -- reserve-sizing aid, not a filing-accuracy tool. "
             "Expected error ~5-10%; confirm actual liability with your CPA/K-1.",
    )


def reserve_with_yield(principal: float, annual_yield_rate: float, days: int) -> float:
    """Simple interest accrual on a reserve balance held in e.g. T-bills instead of
    cash. Simple (not compounded) interest is intentional -- this is a rough
    planning estimate, not a real bond-math model."""
    return principal * annual_yield_rate * (days / 365.0)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def ensure_tables():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS k1_trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ptp        TEXT NOT NULL,
                gain       REAL NOT NULL,
                note       TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS k1_rate_config (
                id                     INTEGER PRIMARY KEY CHECK (id = 1),
                federal_ordinary_rate  REAL NOT NULL,
                federal_lt_rate        REAL NOT NULL,
                niit_rate              REAL NOT NULL,
                state_rate             REAL NOT NULL,
                city_rate              REAL NOT NULL,
                section_1256_lt_fraction REAL NOT NULL,
                section_1256_st_fraction REAL NOT NULL,
                updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS k1_safe_harbor_history (
                tax_year          INTEGER PRIMARY KEY,
                total_tax_liability REAL NOT NULL,
                updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS k1_payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_date TEXT NOT NULL,
                amount       REAL NOT NULL,
                tax_year     INTEGER NOT NULL,
                note         TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS k1_reserves (
                bucket       TEXT PRIMARY KEY CHECK (bucket IN ('A', 'B')),
                balance      REAL NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


def add_trade(trade_date: date, ptp: str, gain: float, note: str = ""):
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO k1_trades (trade_date, ptp, gain, note) VALUES (?, ?, ?, ?)",
            (trade_date.isoformat(), ptp, gain, note),
        )


def get_trades(year: int | None = None) -> list[Trade]:
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT trade_date, ptp, gain, note FROM k1_trades ORDER BY trade_date").fetchall()
    trades = [Trade(date.fromisoformat(r[0]), r[1], r[2], r[3] or "") for r in rows]
    if year is not None:
        trades = [t for t in trades if t.trade_date.year == year]
    return trades


def save_rate_config(rates: RateConfig):
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO k1_rate_config
                (id, federal_ordinary_rate, federal_lt_rate, niit_rate, state_rate, city_rate,
                 section_1256_lt_fraction, section_1256_st_fraction, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                federal_ordinary_rate=excluded.federal_ordinary_rate,
                federal_lt_rate=excluded.federal_lt_rate,
                niit_rate=excluded.niit_rate,
                state_rate=excluded.state_rate,
                city_rate=excluded.city_rate,
                section_1256_lt_fraction=excluded.section_1256_lt_fraction,
                section_1256_st_fraction=excluded.section_1256_st_fraction,
                updated_at=datetime('now')
        """, (rates.federal_ordinary_rate, rates.federal_lt_rate, rates.niit_rate,
              rates.state_rate, rates.city_rate, rates.section_1256_lt_fraction,
              rates.section_1256_st_fraction))


def load_rate_config() -> RateConfig:
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT federal_ordinary_rate, federal_lt_rate, niit_rate, state_rate, city_rate,
                   section_1256_lt_fraction, section_1256_st_fraction
            FROM k1_rate_config WHERE id = 1
        """).fetchone()
    if row is None:
        return RateConfig()  # defaults
    return RateConfig(*row)


def save_prior_year_liability(tax_year: int, total_tax_liability: float):
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO k1_safe_harbor_history (tax_year, total_tax_liability)
            VALUES (?, ?)
            ON CONFLICT(tax_year) DO UPDATE SET
                total_tax_liability=excluded.total_tax_liability, updated_at=datetime('now')
        """, (tax_year, total_tax_liability))


def get_prior_year_liability(tax_year: int) -> float | None:
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT total_tax_liability FROM k1_safe_harbor_history WHERE tax_year = ?", (tax_year,)
        ).fetchone()
    return row[0] if row else None


def add_payment(payment_date: date, amount: float, tax_year: int, note: str = ""):
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO k1_payments (payment_date, amount, tax_year, note) VALUES (?, ?, ?, ?)",
            (payment_date.isoformat(), amount, tax_year, note),
        )


def get_payments(tax_year: int) -> list[tuple[date, float]]:
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT payment_date, amount FROM k1_payments WHERE tax_year = ? ORDER BY payment_date",
            (tax_year,),
        ).fetchall()
    return [(date.fromisoformat(r[0]), r[1]) for r in rows]


def set_reserve_balance(bucket: str, balance: float):
    assert bucket in ("A", "B")
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO k1_reserves (bucket, balance, updated_at) VALUES (?, ?, datetime('now'))
            ON CONFLICT(bucket) DO UPDATE SET balance=excluded.balance, updated_at=datetime('now')
        """, (bucket, balance))


def get_reserve_balance(bucket: str) -> float:
    assert bucket in ("A", "B")
    ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT balance FROM k1_reserves WHERE bucket = ?", (bucket,)).fetchone()
    return row[0] if row else 0.0


# ---------------------------------------------------------------------------
# Full report (composes everything above against persisted state)
# ---------------------------------------------------------------------------

def build_report(year: int, buffer_quarters: int = 4) -> dict:
    """Composes the full indicative report for a tax year: per-PTP annual gain,
    blended rate, Bucket A/B needed vs reserved, safe-harbor quarterly schedule,
    next due date. Always label output as indicative -- see module docstring."""
    trades = get_trades(year=year)
    rates = load_rate_config()
    net_by_ptp = annual_net_gain_by_ptp(trades, year)

    per_ptp = {}
    total_bucket_a_needed = 0.0
    for ptp, net_gain in net_by_ptp.items():
        tax_due = bucket_a_tax_due(net_gain, rates)
        per_ptp[ptp] = {
            "net_gain": net_gain,
            "bucket_a_tax_due": tax_due,
            "suspended_loss_carryforward": suspended_loss_carryforward(net_gain),
        }
        total_bucket_a_needed += tax_due

    prior_liability = get_prior_year_liability(year - 1) or 0.0
    updated_liability = prior_liability + total_bucket_a_needed
    bucket_b_needed = bucket_b_step_up(prior_liability, updated_liability, buffer_quarters)

    bucket_a = BucketStatus(needed=total_bucket_a_needed, reserved=get_reserve_balance("A"))
    bucket_b = BucketStatus(needed=bucket_b_needed, reserved=get_reserve_balance("B"))

    this_year_prior_liability = get_prior_year_liability(year)
    schedule = []
    if this_year_prior_liability is not None:
        schedule = safe_harbor_schedule(year, this_year_prior_liability, get_payments(year))
    upcoming = next_due(schedule) if schedule else None

    return {
        "year": year,
        "blended_rate": rates.blended_rate(),
        "per_ptp": per_ptp,
        "bucket_a": bucket_a,
        "bucket_b": bucket_b,
        "safe_harbor_schedule": schedule,
        "next_due": upcoming,
        "note": "INDICATIVE ONLY -- actual liability depends on K-1 Section 1256 "
                "reporting; confirm against the K-1 when received. Not a substitute "
                "for CPA-prepared numbers.",
    }
