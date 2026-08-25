#!/usr/bin/env python3
"""Download one NEPSE Today Price table through the public website UI.

The official API rejected direct requests during earlier testing. This program
uses a real headless Chromium browser, selects the requested date through the
website's calendar, chooses 500 items, presses Filter, and writes the visible
official table to CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_URL = "https://www.nepalstock.com/today-price"
MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}


class PriceDownloadError(RuntimeError):
    """Raised when the selected price table cannot be validated safely."""


@dataclass(frozen=True)
class PriceStatus:
    requested_date: str
    selected_date: str | None
    status: str
    rows_downloaded: int
    csv_file: str | None
    message: str
    generated_at_utc: str


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use YYYY-MM-DD, for example 2026-08-25."
        ) from exc


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_page_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def month_difference(current_year: int, current_month: int, target: date) -> int:
    return (target.year - current_year) * 12 + (target.month - current_month)


def choose_calendar_date(page: Any, requested: date) -> str:
    date_box = page.locator('main input[bsdatepicker][type="text"]').first
    date_box.wait_for(state="visible", timeout=60_000)

    current_value = date_box.input_value().strip()
    if parse_page_date(current_value) == requested:
        return current_value

    date_box.click()
    calendar = page.locator("bs-datepicker-container")
    calendar.wait_for(state="visible", timeout=30_000)

    for _ in range(180):
        current_buttons = calendar.locator(
            "bs-datepicker-navigation-view button.current"
        )
        if current_buttons.count() < 2:
            raise PriceDownloadError("The NEPSE calendar heading was not found.")

        month_name = clean_text(current_buttons.nth(0).inner_text())
        year_text = clean_text(current_buttons.nth(1).inner_text())
        if month_name not in MONTHS or not year_text.isdigit():
            raise PriceDownloadError(
                f"Unexpected NEPSE calendar heading: {month_name} {year_text}"
            )

        difference = month_difference(int(year_text), MONTHS[month_name], requested)
        if difference == 0:
            break
        navigation_button = "button.previous" if difference < 0 else "button.next"
        button = calendar.locator(navigation_button)
        if not button.is_enabled():
            raise PriceDownloadError(
                f"The calendar cannot move to {requested.isoformat()}."
            )
        button.click()
        page.wait_for_timeout(150)
    else:
        raise PriceDownloadError("The requested date is outside the calendar limit.")

    day = calendar.locator(
        "span[bsdatepickerdaydecorator]:not(.is-other-month):not(.disabled)"
    ).filter(has_text=re.compile(rf"^{requested.day}$"))
    if day.count() != 1:
        raise PriceDownloadError(
            f"Could not select {requested.isoformat()} in the NEPSE calendar."
        )
    day.first.click()

    selected_value = date_box.input_value().strip()
    if parse_page_date(selected_value) != requested:
        raise PriceDownloadError(
            f"The calendar selected {selected_value!r}, not {requested.isoformat()}."
        )
    return selected_value


def validate_table(headers: list[str], rows: list[list[str]]) -> None:
    normalized_headers = [clean_text(item).lower() for item in headers]
    if "symbol" not in normalized_headers:
        raise PriceDownloadError("The official price table has no Symbol column.")
    if len(headers) < 10:
        raise PriceDownloadError(
            f"The official price table returned only {len(headers)} columns."
        )
    if not rows:
        raise PriceDownloadError("The price table contained no security rows.")
    if len(rows) >= 500:
        raise PriceDownloadError(
            "The table reached the 500-row safety limit and may require pagination."
        )

    symbol_index = normalized_headers.index("symbol")
    symbols: list[str] = []
    for expected_sequence, row in enumerate(rows, start=1):
        if len(row) != len(headers):
            raise PriceDownloadError(
                f"Price row {expected_sequence} has {len(row)} columns; "
                f"expected {len(headers)}."
            )
        try:
            sequence = int(row[0].replace(",", ""))
        except ValueError as exc:
            raise PriceDownloadError(
                f"Price row {expected_sequence} has an invalid SN value {row[0]!r}."
            ) from exc
        if sequence != expected_sequence:
            raise PriceDownloadError(
                f"Price row sequence jumped from {expected_sequence - 1} to {sequence}."
            )
        symbol = clean_text(row[symbol_index]).upper()
        if not symbol:
            raise PriceDownloadError(f"Price row {expected_sequence} has no symbol.")
        symbols.append(symbol)

    duplicate_count = len(symbols) - len(set(symbols))
    if duplicate_count:
        raise PriceDownloadError(
            f"The official price table returned {duplicate_count} duplicate symbols."
        )


def write_price_csv(
    output_dir: Path,
    requested: date,
    headers: list[str],
    rows: list[list[str]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"price-{requested.isoformat()}.csv"
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Business Date", *headers])
        for row in rows:
            writer.writerow([requested.isoformat(), *row])
    temporary.replace(path)
    return path


def write_status(output_dir: Path, status: PriceStatus) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "price_status.json"
    path.write_text(json.dumps(asdict(status), indent=2), encoding="utf-8")
    return path


def download_price(requested: date, output_dir: Path) -> PriceStatus:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    selected_value: str | None = None
    page = None
    browser = None
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                locale="en-US",
                timezone_id="Asia/Kathmandu",
                viewport={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=120_000)
            page.get_by_role("heading", name="Today's Price", exact=True).wait_for(
                state="visible", timeout=90_000
            )
            page.wait_for_timeout(4_000)

            selected_value = choose_calendar_date(page, requested)
            page.locator("main select").select_option(label="500")
            page.get_by_role("button", name="Filter", exact=True).click()
            try:
                page.wait_for_load_state("networkidle", timeout=90_000)
            except PlaywrightTimeoutError:
                # Live tickers can keep the page busy even after the price table loads.
                pass
            page.wait_for_timeout(3_000)

            selected_after_filter = page.locator(
                'main input[bsdatepicker][type="text"]'
            ).first.input_value()
            if parse_page_date(selected_after_filter) != requested:
                raise PriceDownloadError(
                    f"The filtered page shows {selected_after_filter!r}, not "
                    f"{requested.isoformat()}."
                )

            main_text = page.locator("main").inner_text()
            row_locators = page.locator("main table tbody tr")
            row_count = row_locators.count()
            if re.search(r"No data available", main_text, re.IGNORECASE):
                if row_count:
                    raise PriceDownloadError(
                        "The page reported no data but still displayed security rows."
                    )
                return PriceStatus(
                    requested.isoformat(),
                    selected_after_filter,
                    "NOT_AVAILABLE",
                    0,
                    None,
                    "No Today Price rows were displayed (holiday or non-trading day).",
                    generated_at,
                )

            headers = [
                clean_text(value)
                for value in page.locator("main table thead th").all_inner_texts()
            ]
            rows: list[list[str]] = []
            for row_locator in row_locators.all():
                cells = [
                    clean_text(value)
                    for value in row_locator.locator("td").all_inner_texts()
                ]
                if cells:
                    rows.append(cells)

            validate_table(headers, rows)
            csv_path = write_price_csv(output_dir, requested, headers, rows)
            return PriceStatus(
                requested.isoformat(),
                selected_after_filter,
                "DOWNLOADED",
                len(rows),
                csv_path.name,
                "Official NEPSE price table downloaded and validated.",
                generated_at,
            )
    except Exception as exc:
        if page is not None:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(output_dir / "price_failure.png"), full_page=True
                )
                (output_dir / "price_failure.html").write_text(
                    page.content(), encoding="utf-8"
                )
            except Exception:
                pass
        return PriceStatus(
            requested.isoformat(),
            selected_value,
            "FAILED",
            0,
            None,
            f"{type(exc).__name__}: {exc}",
            generated_at,
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def self_test() -> int:
    headers = [
        "SN",
        "Symbol",
        "Close Price* (Rs)",
        "Open Price (Rs)",
        "High Price (Rs)",
        "Low Price (Rs)",
        "Total Traded Quantity",
        "Total Traded Value",
        "Total Trades",
        "LTP",
    ]
    rows = [
        ["1", "AAA", "10", "9", "11", "8", "100", "1000", "5", "10"],
        ["2", "BBB", "20", "19", "21", "18", "200", "4000", "8", "20"],
    ]
    validate_table(headers, rows)
    assert month_difference(2026, 8, date(2026, 8, 1)) == 0
    assert month_difference(2026, 8, date(2025, 12, 1)) == -8
    assert parse_page_date("08/25/2026") == date(2026, 8, 25)
    with tempfile.TemporaryDirectory() as temporary:
        path = write_price_csv(Path(temporary), date(2026, 8, 25), headers, rows)
        assert path.is_file()
        assert "Business Date" in path.read_text(encoding="utf-8-sig")
    print("Price downloader self-test passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=parse_iso_date)
    parser.add_argument("--output-dir", type=Path, default=Path("output/price"))
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return self_test()
    if args.date is None:
        print("--date is required unless --self-test is used.", file=sys.stderr)
        return 2

    status = download_price(args.date, args.output_dir)
    status_path = write_status(args.output_dir, status)
    print(json.dumps(asdict(status), indent=2))
    print("Price status:", status_path)
    return 1 if status.status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
