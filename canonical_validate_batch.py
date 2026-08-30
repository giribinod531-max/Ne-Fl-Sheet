#!/usr/bin/env python3
"""Independently validate canonical CSVs and their batch quality report."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


CANONICAL_COLUMNS = [
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
ALLOWED_STATUSES = {"COMPLETE", "MINOR_GAP", "REJECT", "NOT_AVAILABLE", "FAILED"}


class ValidationError(RuntimeError):
    pass


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def expected_dates(start: date, end: date) -> list[str]:
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def read_report(output_dir: Path, start: date, end: date) -> dict[str, dict[str, str]]:
    report_path = output_dir / "quality_report.csv"
    if not report_path.is_file():
        raise ValidationError("quality_report.csv is missing.")
    with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    report = {row.get("requested_date", ""): row for row in rows}
    wanted = expected_dates(start, end)
    if list(report) != wanted:
        raise ValidationError(
            "The quality report does not contain every requested date in order."
        )
    invalid = sorted({row.get("status", "") for row in rows} - ALLOWED_STATUSES)
    if invalid:
        raise ValidationError(f"Unknown quality status: {invalid}.")
    return report


def decimal_value(value: str, label: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValidationError(f"Invalid {label}: {value!r}.") from exc


def broker_value_is_valid(value: str) -> bool:
    value = value.strip()
    return bool(re.fullmatch(r"[0-9]+", value)) and int(value) > 0


def validate_csv(path: Path, requested: str, expected_rows: int) -> dict[str, object]:
    row_count = 0
    transaction_ids: set[str] = set()
    total_quantity = 0
    total_amount = Decimal("0")
    broker_anomaly_rows = 0
    expected_prefix = requested.replace("-", "")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CANONICAL_COLUMNS:
            raise ValidationError(
                f"{path.name}: columns are {reader.fieldnames}, expected {CANONICAL_COLUMNS}."
            )
        for row_count, row in enumerate(reader, start=1):
            if row["#"] != str(row_count):
                raise ValidationError(f"{path.name}: serial number fails at row {row_count}.")
            if row["Date"] != requested:
                raise ValidationError(f"{path.name}: wrong Date value at row {row_count}.")
            transaction_id = row["Transact. No."].strip()
            if transaction_id in transaction_ids:
                raise ValidationError(f"{path.name}: duplicate transaction {transaction_id}.")
            transaction_ids.add(transaction_id)
            if re.fullmatch(r"\d{8,}", transaction_id) and not transaction_id.startswith(
                expected_prefix
            ):
                raise ValidationError(
                    f"{path.name}: transaction {transaction_id} belongs to another date."
                )
            quantity = int(row["Quantity"].replace(",", ""))
            rate = decimal_value(row["Rate"], "rate")
            amount = decimal_value(row["Amount"], "amount")
            if quantity <= 0 or rate <= 0 or amount <= 0:
                raise ValidationError(f"{path.name}: non-positive value at row {row_count}.")
            if abs((Decimal(quantity) * rate) - amount) > Decimal("0.01"):
                raise ValidationError(f"{path.name}: quantity × rate mismatch at row {row_count}.")
            if not broker_value_is_valid(row["Buyer"]) or not broker_value_is_valid(
                row["Seller"]
            ):
                broker_anomaly_rows += 1
            total_quantity += quantity
            total_amount += amount

    if row_count != expected_rows:
        raise ValidationError(
            f"{path.name}: {row_count:,} rows, but report states {expected_rows:,}."
        )
    return {
        "date": requested,
        "rows": row_count,
        "quantity": total_quantity,
        "amount": str(total_amount),
        "broker_anomaly_rows": broker_anomaly_rows,
    }


def run(output_dir: Path, start: date, end: date) -> dict[str, object]:
    report = read_report(output_dir, start, end)
    checked: list[dict[str, object]] = []
    for requested, quality in report.items():
        status = quality["status"]
        expected_rows = int(quality["downloaded_unique_rows"] or 0)
        folder = "daily_csv" if status in {"COMPLETE", "MINOR_GAP"} else "rejected_csv"
        csv_path = output_dir / folder / f"{requested}.csv"
        if expected_rows:
            if not csv_path.is_file():
                raise ValidationError(f"{requested}: expected CSV is missing.")
            checked_result = validate_csv(csv_path, requested, expected_rows)
            reported_anomalies = int(quality.get("broker_anomaly_rows") or 0)
            if checked_result["broker_anomaly_rows"] != reported_anomalies:
                raise ValidationError(
                    f"{requested}: independently counted broker anomalies do not "
                    "match quality_report.csv."
                )
            checked.append(checked_result)
        elif csv_path.exists():
            raise ValidationError(f"{requested}: unexpected empty-day CSV exists.")

    summary_path = output_dir / "validation_summary.json"
    if not summary_path.is_file():
        raise ValidationError("validation_summary.json is missing.")
    generated = json.loads(summary_path.read_text(encoding="utf-8"))
    status_counts = {
        status: sum(row["status"] == status for row in report.values())
        for status in sorted(ALLOWED_STATUSES)
    }
    if generated.get("status_counts") != status_counts:
        raise ValidationError("JSON and CSV quality status counts do not match.")
    if generated.get("total_downloaded_rows") != sum(item["rows"] for item in checked):
        raise ValidationError("JSON and independently counted CSV rows do not match.")
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "status_counts": status_counts,
        "validated_csv_files": len(checked),
        "validated_rows": sum(item["rows"] for item in checked),
        "broker_anomaly_rows": sum(item["broker_anomaly_rows"] for item in checked),
        "result": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-date", required=True, type=parse_date)
    parser.add_argument("--end-date", required=True, type=parse_date)
    args = parser.parse_args()
    try:
        result = run(args.output_dir, args.start_date, args.end_date)
    except (ValidationError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"VALIDATION FAILED: {exc}")
        return 1
    result_path = args.output_dir / "independent_validation.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
