#!/usr/bin/env python3
"""Download date-wise Merolagani floorsheets in the canonical analysis format.

The downloader never silently calls a partial day complete. It reconciles the
visible rows against Merolagani's displayed transaction, quantity and amount
totals, then assigns COMPLETE, MINOR_GAP, REJECT, NOT_AVAILABLE or FAILED.
The exported CSV keeps a sequential row number and date but excludes Page;
page diagnostics remain available in the quality report and downloader log.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


FLOOR_URL = "https://www.merolagani.com/Floorsheet.aspx"
SEARCH_EVENT = "ctl00$ContentPlaceHolder1$lbtnSearchFloorsheet"
DATE_FIELD = "ctl00$ContentPlaceHolder1$txtFloorsheetDateFilter"
PAGE_FIELD = "ctl00$ContentPlaceHolder1$PagerControl2$hdnCurrentPage"
PAGE_BUTTON = "ctl00$ContentPlaceHolder1$PagerControl2$btnPaging"
PAGER_ID = "ctl00_ContentPlaceHolder1_PagerControl2_litRecords"
MARKET_DATE_ID = "ctl00_ContentPlaceHolder1_marketDate"
TOTAL_QUANTITY_ID = "ctl00_ContentPlaceHolder1_totalQty"
TOTAL_AMOUNT_ID = "ctl00_ContentPlaceHolder1_totalAmount"

# A day is MINOR_GAP only when every missing measure is at most 0.10%.
MINOR_GAP_LIMIT = Decimal("0.001")
AMOUNT_TOLERANCE = Decimal("0.01")


class FloorsheetError(RuntimeError):
    """Raised when a response cannot be validated safely."""


@dataclass(frozen=True)
class Transaction:
    transaction_no: str
    symbol: str
    buyer: str
    seller: str
    quantity: int
    rate: Decimal
    amount: Decimal
    source_page: int


@dataclass(frozen=True)
class PagerStats:
    first_record: int | None
    last_record: int | None
    total_records: int | None
    total_pages: int
    text: str


@dataclass(frozen=True)
class ShortPage:
    page_number: int
    first_record: int
    last_record: int
    advertised_rows: int
    returned_rows: int


@dataclass(frozen=True)
class DownloadResult:
    requested_date: date
    market_date: date | None
    status: str
    pages: int
    expected_records: int
    expected_quantity: int
    expected_amount: Decimal
    transactions: tuple[Transaction, ...]
    short_pages: tuple[ShortPage, ...]
    message: str

    @property
    def actual_records(self) -> int:
        return len(self.transactions)

    @property
    def actual_quantity(self) -> int:
        return sum(row.quantity for row in self.transactions)

    @property
    def actual_amount(self) -> Decimal:
        return sum((row.amount for row in self.transactions), Decimal("0"))

    @property
    def broker_anomaly_rows(self) -> int:
        return sum(
            not broker_value_is_valid(row.buyer)
            or not broker_value_is_valid(row.seller)
            for row in self.transactions
        )

    @property
    def broker_anomaly_values(self) -> tuple[str, ...]:
        values: set[str] = set()
        for row in self.transactions:
            if not broker_value_is_valid(row.buyer):
                values.add(f"Buyer={row.buyer!r}")
            if not broker_value_is_valid(row.seller):
                values.add(f"Seller={row.seller!r}")
        return tuple(sorted(values))

    @property
    def duplicate_transaction_id_rows(self) -> int:
        transaction_ids = [row.transaction_no for row in self.transactions]
        return len(transaction_ids) - len(set(transaction_ids))

    @property
    def duplicate_transaction_id_values(self) -> tuple[str, ...]:
        counts = Counter(row.transaction_no for row in self.transactions)
        repeated = [
            f"{value!r} x{count:,}"
            for value, count in sorted(counts.items())
            if count > 1
        ]
        return tuple(repeated[:20])

    @property
    def missing_records(self) -> int:
        return self.expected_records - self.actual_records

    @property
    def missing_quantity(self) -> int:
        return self.expected_quantity - self.actual_quantity

    @property
    def missing_amount(self) -> Decimal:
        return self.expected_amount - self.actual_amount


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_integer(value: str) -> int:
    cleaned = clean_text(value).replace(",", "")
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Expected an integer, received {value!r}") from exc
    if number != number.to_integral_value():
        raise ValueError(f"Expected an integer, received {value!r}")
    return int(number)


def normalize_broker(value: str) -> str:
    """Normalize numeric brokers; preserve any source marker without invention."""
    cleaned = clean_text(value)
    try:
        broker = parse_integer(cleaned)
    except ValueError:
        return cleaned
    return str(broker)


def broker_value_is_valid(value: str) -> bool:
    value_text = str(value).strip()
    if re.fullmatch(r"[0-9]+", value_text):
        return int(value_text) > 0
    # NEPSE dealer member codes are alphanumeric; D01 is Nagarik Stock Dealer.
    return bool(re.fullmatch(r"D[0-9]{2}", value_text, re.IGNORECASE))


def parse_decimal(value: str) -> Decimal:
    cleaned = clean_text(value).replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Expected a number, received {value!r}") from exc


def parse_displayed_number(value: str) -> Decimal:
    match = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", clean_text(value))
    if not match:
        raise ValueError(f"No number found in {value!r}")
    return parse_decimal(match.group(0))


def decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def difference_percent(difference: int | Decimal, expected: int | Decimal) -> Decimal:
    if expected == 0:
        return Decimal("0") if difference == 0 else Decimal("Infinity")
    return (Decimal(difference) / Decimal(expected)) * Decimal("100")


def parse_market_date(text: str) -> date | None:
    """Parse dates such as 'As of 2026/08/21 03:00:00'."""
    cleaned = clean_text(text)
    patterns = (
        (r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", (3, 1, 2)),
    )
    for pattern, order in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        try:
            return date(*(int(match.group(index)) for index in order))
        except ValueError:
            continue
    return None


def extract_market_date(soup: Any) -> tuple[str, date | None]:
    element = soup.find(id=MARKET_DATE_ID)
    text = clean_text(element.get_text(" ", strip=True)) if element else ""
    return text, parse_market_date(text)


def extract_daily_totals(soup: Any) -> tuple[int | None, Decimal | None]:
    quantity_element = soup.find(id=TOTAL_QUANTITY_ID)
    amount_element = soup.find(id=TOTAL_AMOUNT_ID)
    if quantity_element is None or amount_element is None:
        return None, None
    try:
        quantity_number = parse_displayed_number(
            quantity_element.get_text(" ", strip=True)
        )
        if quantity_number != quantity_number.to_integral_value():
            raise ValueError("Displayed total quantity is not an integer.")
        amount = parse_displayed_number(amount_element.get_text(" ", strip=True))
    except ValueError as exc:
        raise FloorsheetError(f"Could not parse displayed daily totals: {exc}") from exc
    return int(quantity_number), amount


def extract_pager_stats(soup: Any) -> PagerStats:
    element = soup.find(id=PAGER_ID)
    text = clean_text(element.get_text(" ", strip=True)) if element else ""
    range_match = re.search(
        r"Showing\s+([0-9,]+)\s*-\s*([0-9,]+)\s+of\s+([0-9,]+)\s+records",
        text,
        re.IGNORECASE,
    )
    pages_match = re.search(
        r"Total\s+pages\s*:\s*([0-9,]+)", text, re.IGNORECASE
    )

    first_record = last_record = total_records = None
    if range_match:
        first_record = int(range_match.group(1).replace(",", ""))
        last_record = int(range_match.group(2).replace(",", ""))
        total_records = int(range_match.group(3).replace(",", ""))
    total_pages = int(pages_match.group(1).replace(",", "")) if pages_match else 0
    return PagerStats(first_record, last_record, total_records, total_pages, text)


def extract_transactions(soup: Any, page_number: int) -> list[Transaction]:
    table = soup.select_one("table.sortable")
    if table is None:
        return []
    tbody = table.find("tbody")
    if tbody is None:
        return []

    transactions: list[Transaction] = []
    for row in tbody.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 8:
            continue
        values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells[:8]]
        try:
            transaction = Transaction(
                transaction_no=values[1],
                symbol=values[2].upper(),
                buyer=normalize_broker(values[3]),
                seller=normalize_broker(values[4]),
                quantity=parse_integer(values[5]),
                rate=parse_decimal(values[6]),
                amount=parse_decimal(values[7]),
                source_page=page_number,
            )
        except (ValueError, IndexError) as exc:
            raise FloorsheetError(
                f"Unexpected values on page {page_number}: {values!r}"
            ) from exc
        if transaction.transaction_no and transaction.symbol:
            transactions.append(transaction)
    return transactions


def read_form_fields(soup: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    excluded_types = {"submit", "button", "image", "file", "checkbox", "radio"}
    for element in soup.find_all("input"):
        name = element.get("name")
        input_type = str(element.get("type", "")).lower()
        if name and input_type not in excluded_types:
            fields[str(name)] = str(element.get("value", ""))
    return fields


def build_session() -> Any:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/124 Safari/537.36 NEPSE-personal-research/1.0"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": FLOOR_URL,
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def request_html(
    session: Any,
    method: str,
    data: dict[str, str] | None = None,
) -> Any:
    from bs4 import BeautifulSoup

    if method == "GET":
        response = session.get(FLOOR_URL, timeout=(30, 180))
    else:
        response = session.post(FLOOR_URL, data=data, timeout=(30, 180))
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", "").lower():
        raise FloorsheetError("Merolagani did not return an HTML page.")
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.find("input", {"name": "__VIEWSTATE"}) is None:
        raise FloorsheetError(
            "The expected ASP.NET form was not found. The site may have changed "
            "or blocked this request."
        )
    return soup


def inspect_page_position(
    pager: PagerStats,
    page_number: int,
    page_rows: int,
    total_pages: int,
    advertised_page_size: int,
) -> ShortPage | None:
    if pager.total_pages != total_pages:
        raise FloorsheetError(
            f"Page count changed from {total_pages} to {pager.total_pages}."
        )
    if (
        pager.first_record is None
        or pager.last_record is None
        or pager.total_records is None
    ):
        raise FloorsheetError(f"Could not read pager details on page {page_number}.")

    expected_first = (page_number - 1) * advertised_page_size + 1
    if pager.first_record != expected_first:
        raise FloorsheetError(
            f"Requested page {page_number}, but the site reports records starting "
            f"at {pager.first_record} instead of {expected_first}."
        )
    advertised_rows = pager.last_record - pager.first_record + 1
    if page_rows == advertised_rows:
        return None
    return ShortPage(
        page_number,
        pager.first_record,
        pager.last_record,
        advertised_rows,
        page_rows,
    )


def validate_identifiers(requested: date, transactions: list[Transaction]) -> None:
    transaction_ids = [row.transaction_no for row in transactions]
    expected_prefix = requested.strftime("%Y%m%d")
    date_like_ids = [value for value in transaction_ids if re.fullmatch(r"\d{8,}", value)]
    wrong_date_ids = [value for value in date_like_ids if not value.startswith(expected_prefix)]
    if wrong_date_ids:
        raise FloorsheetError(
            "Transaction IDs indicate a different market date; first example: "
            f"{wrong_date_ids[0]}."
        )


def classify_quality(
    expected_records: int,
    expected_quantity: int,
    expected_amount: Decimal,
    transactions: list[Transaction],
    short_pages: list[ShortPage],
) -> tuple[str, str]:
    actual_records = len(transactions)
    actual_quantity = sum(row.quantity for row in transactions)
    actual_amount = sum((row.amount for row in transactions), Decimal("0"))
    broker_anomaly_rows = sum(
        not broker_value_is_valid(row.buyer)
        or not broker_value_is_valid(row.seller)
        for row in transactions
    )
    duplicate_transaction_id_rows = len(transactions) - len(
        {row.transaction_no for row in transactions}
    )
    differences = (
        expected_records - actual_records,
        expected_quantity - actual_quantity,
        expected_amount - actual_amount,
    )

    if expected_records <= 0 or expected_quantity <= 0 or expected_amount <= 0:
        return "REJECT", "The displayed daily totals are missing or invalid."
    if any(value < 0 for value in differences):
        return "REJECT", "Downloaded values exceed the site's displayed daily totals."

    records_difference, quantity_difference, amount_difference = differences
    amount_is_equal = abs(amount_difference) <= AMOUNT_TOLERANCE
    if (
        records_difference == 0
        and quantity_difference == 0
        and amount_is_equal
        and not short_pages
        and broker_anomaly_rows == 0
        and duplicate_transaction_id_rows == 0
    ):
        return "COMPLETE", "All rows, quantity and amount match the displayed totals."

    if duplicate_transaction_id_rows:
        return (
            "REJECT",
            f"The source contains {duplicate_transaction_id_rows:,} duplicate "
            "transaction-number row(s). The original rows are preserved, but do "
            "not use this day for exact transaction or broker analysis.",
        )

    broker_anomaly_ratio = Decimal(broker_anomaly_rows) / Decimal(expected_records)
    ratios = (
        Decimal(records_difference) / Decimal(expected_records),
        Decimal(quantity_difference) / Decimal(expected_quantity),
        amount_difference / expected_amount,
        broker_anomaly_ratio,
    )
    if max(ratios) <= MINOR_GAP_LIMIT:
        if broker_anomaly_rows:
            return (
                "MINOR_GAP",
                f"The source contains {broker_anomaly_rows:,} row(s) with a missing "
                "or non-numeric broker identifier. The original values are preserved; "
                "every measured quality difference is at most 0.10%.",
            )
        return (
            "MINOR_GAP",
            "The source omitted a small number of rows; every measured difference "
            "is at most 0.10% and is recorded in the quality report.",
        )
    if broker_anomaly_ratio > MINOR_GAP_LIMIT:
        return (
            "REJECT",
            f"The source contains {broker_anomaly_rows:,} row(s) with a missing or "
            "non-numeric broker identifier, above the safe 0.10% limit. The "
            "original values are preserved, but do not use this day for exact "
            "broker analysis.",
        )
    return (
        "REJECT",
        "The source discrepancy is above the safe 0.10% limit; do not use this "
        "day for analysis or machine learning.",
    )


def empty_result(
    requested: date, market_date: date | None, status: str, message: str
) -> DownloadResult:
    return DownloadResult(
        requested,
        market_date,
        status,
        0,
        0,
        0,
        Decimal("0"),
        tuple(),
        tuple(),
        message,
    )


def download_one_date(
    session: Any, requested: date, delay_seconds: float
) -> DownloadResult:
    requested_text = requested.strftime("%m/%d/%Y")
    logging.info("Searching %s", requested_text)

    soup = request_html(session, "GET")
    fields = read_form_fields(soup)
    fields["__EVENTTARGET"] = SEARCH_EVENT
    fields["__EVENTARGUMENT"] = ""
    fields[DATE_FIELD] = requested_text
    soup = request_html(session, "POST", fields)

    market_text, market_date = extract_market_date(soup)
    first_page = extract_transactions(soup, 1)
    if not first_page:
        return empty_result(
            requested,
            market_date,
            "NOT_AVAILABLE",
            "No floorsheet rows returned (weekend, holiday or unavailable date).",
        )
    if market_date is None:
        raise FloorsheetError(
            f"Could not read the market date from the page text {market_text!r}."
        )
    if market_date != requested:
        return empty_result(
            requested,
            market_date,
            "REJECT",
            f"The site returned {market_date.isoformat()} instead of the requested date.",
        )

    pager = extract_pager_stats(soup)
    if (
        pager.first_record is None
        or pager.last_record is None
        or pager.total_records is None
        or pager.total_pages <= 0
    ):
        raise FloorsheetError(f"Could not read pager details: {pager.text!r}")
    expected_quantity, expected_amount = extract_daily_totals(soup)
    if expected_quantity is None or expected_amount is None:
        raise FloorsheetError("Could not read the site's displayed quantity and amount totals.")

    total_pages = pager.total_pages
    expected_records = pager.total_records
    advertised_page_size = pager.last_record - pager.first_record + 1
    if advertised_page_size <= 0:
        raise FloorsheetError("The advertised page size is invalid.")

    transactions = list(first_page)
    short_pages: list[ShortPage] = []
    first_gap = inspect_page_position(
        pager, 1, len(first_page), total_pages, advertised_page_size
    )
    if first_gap:
        short_pages.append(first_gap)
        logging.warning(
            "Page 1 advertised %s rows but returned %s.",
            first_gap.advertised_rows,
            first_gap.returned_rows,
        )
    logging.info(
        "%s | page 1/%s | collected=%s | expected=%s",
        requested.isoformat(),
        total_pages,
        len(transactions),
        expected_records,
    )

    for page_number in range(2, total_pages + 1):
        time.sleep(delay_seconds)
        fields = read_form_fields(soup)
        fields["__EVENTTARGET"] = ""
        fields["__EVENTARGUMENT"] = ""
        fields[PAGE_FIELD] = str(page_number)
        fields[PAGE_BUTTON] = ""
        soup = request_html(session, "POST", fields)
        page_rows = extract_transactions(soup, page_number)
        if not page_rows:
            raise FloorsheetError(
                f"No transaction rows found on page {page_number}/{total_pages}."
            )
        current_pager = extract_pager_stats(soup)
        gap = inspect_page_position(
            current_pager,
            page_number,
            len(page_rows),
            total_pages,
            advertised_page_size,
        )
        if gap:
            short_pages.append(gap)
            logging.warning(
                "Page %s advertised %s rows but returned %s.",
                page_number,
                gap.advertised_rows,
                gap.returned_rows,
            )
        transactions.extend(page_rows)
        if page_number == total_pages or page_number % 10 == 0:
            logging.info(
                "%s | page %s/%s | collected=%s",
                requested.isoformat(),
                page_number,
                total_pages,
                len(transactions),
            )

    validate_identifiers(requested, transactions)
    status, message = classify_quality(
        expected_records,
        expected_quantity,
        expected_amount,
        transactions,
        short_pages,
    )
    broker_anomaly_rows = sum(
        not broker_value_is_valid(row.buyer)
        or not broker_value_is_valid(row.seller)
        for row in transactions
    )
    if broker_anomaly_rows:
        anomaly_values = sorted(
            {
                value
                for row in transactions
                for value in (
                    f"Buyer={row.buyer!r}" if not broker_value_is_valid(row.buyer) else "",
                    f"Seller={row.seller!r}" if not broker_value_is_valid(row.seller) else "",
                )
                if value
            }
        )
        logging.warning(
            "%s | preserved %s broker-anomaly row(s) | values=%s",
            requested.isoformat(),
            broker_anomaly_rows,
            anomaly_values,
        )
    duplicate_transaction_id_rows = len(transactions) - len(
        {row.transaction_no for row in transactions}
    )
    if duplicate_transaction_id_rows:
        duplicate_values = Counter(row.transaction_no for row in transactions)
        repeated = [
            f"{value!r} x{count:,}"
            for value, count in duplicate_values.most_common(10)
            if count > 1
        ]
        logging.warning(
            "%s | preserved %s duplicate transaction-number row(s) | values=%s",
            requested.isoformat(),
            duplicate_transaction_id_rows,
            repeated,
        )
    return DownloadResult(
        requested,
        market_date,
        status,
        total_pages,
        expected_records,
        expected_quantity,
        expected_amount,
        tuple(transactions),
        tuple(short_pages),
        message,
    )


def write_daily_csv(output_dir: Path, result: DownloadResult) -> Path:
    folder_name = (
        "daily_csv" if result.status in {"COMPLETE", "MINOR_GAP"} else "rejected_csv"
    )
    daily_dir = output_dir / folder_name
    daily_dir.mkdir(parents=True, exist_ok=True)
    final_path = daily_dir / f"{result.requested_date.isoformat()}.csv"
    temporary_path = final_path.with_suffix(".csv.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "#",
                "Transact. No.",
                "Symbol",
                "Buyer",
                "Seller",
                "Quantity",
                "Rate",
                "Amount",
                "Date",
            ]
        )
        for sequence, row in enumerate(result.transactions, start=1):
            writer.writerow(
                [
                    sequence,
                    row.transaction_no,
                    row.symbol,
                    row.buyer,
                    row.seller,
                    row.quantity,
                    decimal_text(row.rate),
                    decimal_text(row.amount),
                    result.requested_date.isoformat(),
                ]
            )
    os.replace(temporary_path, final_path)
    return final_path


def iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use YYYY-MM-DD, for example 2026-08-21."
        ) from exc


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "downloader.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def serialize_result(result: DownloadResult) -> dict[str, object]:
    return {
        "requested_date": result.requested_date.isoformat(),
        "market_date": result.market_date.isoformat() if result.market_date else None,
        "status": result.status,
        "pages": result.pages,
        "expected_records": result.expected_records,
        "downloaded_unique_rows": result.actual_records,
        "broker_anomaly_rows": result.broker_anomaly_rows,
        "broker_anomaly_percent": decimal_text(
            difference_percent(result.broker_anomaly_rows, result.expected_records)
        ),
        "broker_anomaly_values": list(result.broker_anomaly_values),
        "duplicate_transaction_id_rows": result.duplicate_transaction_id_rows,
        "duplicate_transaction_id_percent": decimal_text(
            difference_percent(
                result.duplicate_transaction_id_rows, result.expected_records
            )
        ),
        "duplicate_transaction_id_values": list(
            result.duplicate_transaction_id_values
        ),
        "missing_rows": result.missing_records,
        "missing_rows_percent": decimal_text(
            difference_percent(result.missing_records, result.expected_records)
        ),
        "expected_quantity": result.expected_quantity,
        "downloaded_quantity": result.actual_quantity,
        "missing_quantity": result.missing_quantity,
        "missing_quantity_percent": decimal_text(
            difference_percent(result.missing_quantity, result.expected_quantity)
        ),
        "expected_amount": decimal_text(result.expected_amount),
        "downloaded_amount": decimal_text(result.actual_amount),
        "missing_amount": decimal_text(result.missing_amount),
        "missing_amount_percent": decimal_text(
            difference_percent(result.missing_amount, result.expected_amount)
        ),
        "short_pages": [
            {
                "page": item.page_number,
                "advertised_range": f"{item.first_record}-{item.last_record}",
                "advertised_rows": item.advertised_rows,
                "returned_rows": item.returned_rows,
            }
            for item in result.short_pages
        ],
        "message": result.message,
    }


def write_quality_report(output_dir: Path, results: list[DownloadResult]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "quality_report.csv"
    columns = [
        "requested_date",
        "market_date",
        "status",
        "pages",
        "expected_records",
        "downloaded_unique_rows",
        "broker_anomaly_rows",
        "broker_anomaly_percent",
        "broker_anomaly_values",
        "duplicate_transaction_id_rows",
        "duplicate_transaction_id_percent",
        "duplicate_transaction_id_values",
        "missing_rows",
        "missing_rows_percent",
        "expected_quantity",
        "downloaded_quantity",
        "missing_quantity",
        "missing_quantity_percent",
        "expected_amount",
        "downloaded_amount",
        "missing_amount",
        "missing_amount_percent",
        "short_page_numbers",
        "message",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            values = serialize_result(result)
            writer.writerow(
                {
                    **{
                        key: values[key]
                        for key in columns
                        if key
                        not in {
                            "short_page_numbers",
                            "broker_anomaly_values",
                            "duplicate_transaction_id_values",
                        }
                    },
                    "short_page_numbers": ";".join(
                        str(item.page_number) for item in result.short_pages
                    ),
                    "broker_anomaly_values": ";".join(result.broker_anomaly_values),
                    "duplicate_transaction_id_values": ";".join(
                        result.duplicate_transaction_id_values
                    ),
                }
            )
    return path


def run(start: date, end: date, output_dir: Path, delay_seconds: float) -> int:
    import requests

    if end < start:
        raise ValueError("End date cannot be before start date.")
    configure_logging(output_dir / "logs")
    session = build_session()
    results: list[DownloadResult] = []

    try:
        all_dates = list(iter_dates(start, end))
        for position, requested in enumerate(all_dates, start=1):
            logging.info("Date %s/%s: %s", position, len(all_dates), requested)
            try:
                result = download_one_date(session, requested, delay_seconds)
            except (requests.RequestException, FloorsheetError, OSError) as exc:
                logging.exception("Failed %s", requested)
                result = empty_result(
                    requested,
                    None,
                    "FAILED",
                    f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if result.transactions:
                csv_path = write_daily_csv(output_dir, result)
                logging.info(
                    "%s | saved %s rows to %s",
                    result.status,
                    result.actual_records,
                    csv_path,
                )
            else:
                logging.info("%s: %s", result.status, result.message)
    finally:
        session.close()

    counts = {
        status: sum(item.status == status for item in results)
        for status in ("COMPLETE", "MINOR_GAP", "REJECT", "NOT_AVAILABLE", "FAILED")
    }
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": FLOOR_URL,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "status_counts": counts,
        "usable_dates": counts["COMPLETE"] + counts["MINOR_GAP"],
        "total_downloaded_rows": sum(item.actual_records for item in results),
        "results": [serialize_result(item) for item in results],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    report_path = write_quality_report(output_dir, results)
    logging.info(
        "Finished | complete=%s | minor_gap=%s | reject=%s | unavailable=%s | "
        "failed=%s | rows=%s | report=%s",
        counts["COMPLETE"],
        counts["MINOR_GAP"],
        counts["REJECT"],
        counts["NOT_AVAILABLE"],
        counts["FAILED"],
        summary["total_downloaded_rows"],
        report_path,
    )
    return 1 if counts["REJECT"] or counts["FAILED"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=parse_iso_date)
    parser.add_argument("--end-date", required=True, type=parse_iso_date)
    parser.add_argument("--output-dir", default="output", type=Path)
    parser.add_argument("--delay", default=0.8, type=float)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.delay < 0.5:
        raise SystemExit("Delay must be at least 0.5 seconds.")
    try:
        return run(args.start_date, args.end_date, args.output_dir, args.delay)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        logging.exception("Fatal downloader failure")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
