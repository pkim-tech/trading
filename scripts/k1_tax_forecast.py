"""K-1 / Section 1256 tax forecast CLI -- planning aid only, see k1_tax.py's module
docstring for the tax mechanics modeled. All output is indicative, not a substitute
for CPA-prepared numbers.

Usage:
  .venv/bin/python scripts/k1_tax_forecast.py trade --date 2026-03-14 --ptp AGQ --gain 12500 [--note "..."]
  .venv/bin/python scripts/k1_tax_forecast.py rates --federal-ordinary 0.37 --federal-lt 0.20 \
      --niit 0.038 --state 0.109 --city 0.03876
  .venv/bin/python scripts/k1_tax_forecast.py prior-liability --year 2025 --amount 48000
  .venv/bin/python scripts/k1_tax_forecast.py payment --date 2026-04-15 --amount 12000 --year 2026
  .venv/bin/python scripts/k1_tax_forecast.py reserve --bucket A --amount 15000
  .venv/bin/python scripts/k1_tax_forecast.py report --year 2026 [--buffer-quarters 4] [--stress -20]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import k1_tax


def cmd_trade(args):
    k1_tax.add_trade(date.fromisoformat(args.date), args.ptp, args.gain, args.note or "")
    print(f"Logged: {args.date} {args.ptp} {args.gain:+.2f}  {args.note or ''}")


def cmd_rates(args):
    rates = k1_tax.RateConfig(
        federal_ordinary_rate=args.federal_ordinary,
        federal_lt_rate=args.federal_lt,
        niit_rate=args.niit,
        state_rate=args.state,
        city_rate=args.city,
        section_1256_lt_fraction=args.lt_fraction,
        section_1256_st_fraction=args.st_fraction,
    )
    k1_tax.save_rate_config(rates)
    print(f"Saved rate config. Blended rate: {rates.blended_rate():.2%}")


def cmd_prior_liability(args):
    k1_tax.save_prior_year_liability(args.year, args.amount)
    print(f"Saved {args.year} total tax liability: ${args.amount:,.2f}")


def cmd_payment(args):
    k1_tax.add_payment(date.fromisoformat(args.date), args.amount, args.year, args.note or "")
    print(f"Logged payment: {args.date} ${args.amount:,.2f} (tax year {args.year})")


def cmd_reserve(args):
    k1_tax.set_reserve_balance(args.bucket, args.amount)
    print(f"Set Bucket {args.bucket} reserve balance: ${args.amount:,.2f}")


def cmd_report(args):
    report = k1_tax.build_report(args.year, buffer_quarters=args.buffer_quarters)

    print(f"\n=== K-1 Tax Forecast Report — Tax Year {report['year']} ===")
    print(f"({report['note']})\n")
    print(f"Blended effective rate: {report['blended_rate']:.2%}\n")

    print("-- Per-PTP annual net gain (silo'd, never netted across PTPs) --")
    for ptp, d in report["per_ptp"].items():
        print(f"  {ptp:8s} net_gain=${d['net_gain']:>12,.2f}  bucket_A_tax_due=${d['bucket_a_tax_due']:>12,.2f}"
              + (f"  suspended_loss_carryforward=${d['suspended_loss_carryforward']:,.2f}"
                 if d['suspended_loss_carryforward'] else ""))
    if not report["per_ptp"]:
        print("  (no trades logged for this year)")

    a, b = report["bucket_a"], report["bucket_b"]
    print(f"\n-- Bucket A (tax due at filing) --")
    print(f"  needed=${a.needed:,.2f}  reserved=${a.reserved:,.2f}  "
          f"shortfall=${a.shortfall:,.2f}  fully_reserved={a.fully_reserved}")
    print(f"\n-- Bucket B (safe-harbor step-up, {args.buffer_quarters}/4 quarters buffer) --")
    print(f"  needed=${b.needed:,.2f}  reserved=${b.reserved:,.2f}  "
          f"shortfall=${b.shortfall:,.2f}  fully_reserved={b.fully_reserved}")

    if report["safe_harbor_schedule"]:
        print(f"\n-- Safe-harbor quarterly schedule --")
        for q in report["safe_harbor_schedule"]:
            flag = "  ⚠ BEHIND" if q.behind else ""
            print(f"  {q.label:10s} due={q.due_date}  required_this_quarter=${q.required_amount:,.2f}  "
                  f"cumulative_required=${q.cumulative_required:,.2f}  "
                  f"cumulative_paid=${q.cumulative_paid:,.2f}{flag}")
        nd = report["next_due"]
        if nd:
            days = (nd.due_date - date.today()).days
            print(f"\n  Next due: {nd.label} on {nd.due_date} (${nd.required_amount:,.2f}, {days} days away)")
    else:
        print(f"\n-- Safe-harbor quarterly schedule --\n  (no prior-year liability on file for {report['year']} "
              f"— run `prior-liability --year {report['year']} --amount ...` to enable)")

    if args.stress is not None:
        req_q = report["safe_harbor_schedule"][0].required_amount if report["safe_harbor_schedule"] else 0.0
        st = k1_tax.stress_test_flat_quarter(b.reserved, req_q, trading_delta_pct=args.stress)
        print(f"\n-- Stress test: next quarter trading delta {args.stress:+.0f}% --")
        print(f"  required_quarterly=${st['required_quarterly']:,.2f}  reserved_B=${st['reserved_b']:,.2f}  "
              f"shortfall=${st['shortfall']:,.2f}  "
              f"covered_without_touching_capital={st['covered_without_touching_capital']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("trade", help="log a realized gain/loss trade")
    p.add_argument("--date", required=True)
    p.add_argument("--ptp", required=True)
    p.add_argument("--gain", type=float, required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_trade)

    p = sub.add_parser("rates", help="set the rate engine config")
    p.add_argument("--federal-ordinary", type=float, default=0.37)
    p.add_argument("--federal-lt", type=float, default=0.20)
    p.add_argument("--niit", type=float, default=0.038)
    p.add_argument("--state", type=float, default=0.109)
    p.add_argument("--city", type=float, default=0.03876)
    p.add_argument("--lt-fraction", type=float, default=0.60)
    p.add_argument("--st-fraction", type=float, default=0.40)
    p.set_defaults(func=cmd_rates)

    p = sub.add_parser("prior-liability", help="record a tax year's actual total tax liability")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--amount", type=float, required=True)
    p.set_defaults(func=cmd_prior_liability)

    p = sub.add_parser("payment", help="log an actual estimated-tax payment made")
    p.add_argument("--date", required=True)
    p.add_argument("--amount", type=float, required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_payment)

    p = sub.add_parser("reserve", help="set a reserve bucket's current cash balance")
    p.add_argument("--bucket", choices=["A", "B"], required=True)
    p.add_argument("--amount", type=float, required=True)
    p.set_defaults(func=cmd_reserve)

    p = sub.add_parser("report", help="print the full forecast report")
    p.add_argument("--year", type=int, default=date.today().year)
    p.add_argument("--buffer-quarters", type=int, default=4)
    p.add_argument("--stress", type=float, default=None, help="simulate a trading delta %% for next quarter")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
