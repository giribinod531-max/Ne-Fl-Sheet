#!/usr/bin/env python3
"""Build an exact half-month GitHub Actions matrix for a year range."""

from __future__ import annotations

import argparse
import calendar
import json
from datetime import date, datetime


def build_plan(
    start_year: int, end_year: int, through_date: date | None = None
) -> list[dict[str, str | int]]:
    if start_year < 2014 or end_year > 2026 or start_year > end_year:
        raise ValueError("The supported historical range is 2014 through 2026.")

    final_date = min(through_date or date.today(), date(end_year, 12, 31))
    if final_date < date(start_year, 1, 1):
        raise ValueError("The through-date is before this workflow's year range.")

    plan: list[dict[str, str | int]] = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            last_day = calendar.monthrange(year, month)[1]
            for part, first_day, end_day in (
                (1, 1, 15),
                (2, 16, last_day),
            ):
                start = date(year, month, first_day)
                if start > final_date:
                    continue
                end = min(date(year, month, end_day), final_date)
                plan.append(
                    {
                        "year": year,
                        "month": f"{month:02d}",
                        "part": part,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "batch_id": f"{year}-{month:02d}-P{part}",
                    }
                )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", required=True, type=int)
    parser.add_argument("--end-year", required=True, type=int)
    parser.add_argument(
        "--through-date",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        help="Optional final date. The default is today's date.",
    )
    args = parser.parse_args()

    plan = build_plan(args.start_year, args.end_year, args.through_date)
    if len(plan) > 256:
        raise SystemExit(
            f"Plan contains {len(plan)} jobs; GitHub permits at most 256 matrix jobs."
        )
    print(json.dumps(plan, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
