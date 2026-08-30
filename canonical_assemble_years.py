#!/usr/bin/env python3
"""Combine downloaded half-month artifacts into one audited ZIP per year."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from canonical_backfill_plan import build_plan


BATCH_PATTERN = re.compile(r"canonical-half-(\d{4})-(\d{2})-P([12])$")


def csv_bytes(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def batch_check(batch_dir: Path) -> tuple[bool, str, dict[str, object] | None]:
    check_path = batch_dir / "independent_validation.json"
    if not check_path.is_file():
        return False, "independent_validation.json is missing", None
    try:
        check = json.loads(check_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"independent validation cannot be read: {exc}", None
    if check.get("result") != "PASS":
        return False, "independent validation did not pass", check
    failed_dates = int((check.get("status_counts") or {}).get("FAILED", 0))
    if failed_dates:
        return False, f"batch contains {failed_dates} technically failed date(s)", check
    if not (batch_dir / "quality_report.csv").is_file():
        return False, "quality_report.csv is missing", check
    return True, "validated", check


def assemble(
    input_dir: Path,
    output_dir: Path,
    start_year: int,
    end_year: int,
    through_date: date | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_dirs: dict[tuple[int, int, int], Path] = {}
    for path in input_dir.rglob("canonical-half-*"):
        if not path.is_dir():
            continue
        match = BATCH_PATTERN.fullmatch(path.name)
        if match:
            key = tuple(int(value) for value in match.groups())
            batch_dirs[key] = path

    planned = build_plan(start_year, end_year, through_date)
    planned_by_year: dict[int, list[tuple[int, int, int]]] = {
        year: [] for year in range(start_year, end_year + 1)
    }
    for row in planned:
        planned_by_year[int(row["year"])].append(
            (int(row["year"]), int(row["month"]), int(row["part"]))
        )

    for year in range(start_year, end_year + 1):
        expected_keys = planned_by_year[year]
        uploaded_keys = [key for key in expected_keys if key in batch_dirs]
        valid_keys: list[tuple[int, int, int]] = []
        invalid_batches: list[dict[str, str]] = []
        independent_checks: list[dict[str, object]] = []
        for key in uploaded_keys:
            valid, reason, check = batch_check(batch_dirs[key])
            if valid:
                valid_keys.append(key)
                if check is not None:
                    independent_checks.append(check)
            else:
                invalid_batches.append(
                    {"batch_id": f"{key[0]}-{key[1]:02d}-P{key[2]}", "reason": reason}
                )
        missing_batches = [
            f"{y}-{m:02d}-P{p}" for y, m, p in expected_keys if (y, m, p) not in batch_dirs
        ]
        quality_rows: list[dict[str, str]] = []
        columns: list[str] | None = None
        daily_files: list[Path] = []
        rejected_files: list[Path] = []

        for key in valid_keys:
            batch_dir = batch_dirs[key]
            report_path = batch_dir / "quality_report.csv"
            if report_path.is_file():
                with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    columns = columns or list(reader.fieldnames or [])
                    quality_rows.extend(reader)
            daily_files.extend(sorted((batch_dir / "daily_csv").glob("*.csv")))
            rejected_files.extend(sorted((batch_dir / "rejected_csv").glob("*.csv")))
        quality_rows.sort(key=lambda row: row.get("requested_date", ""))
        statuses = Counter(row.get("status", "UNKNOWN") for row in quality_rows)
        duplicate_dates = [
            value for value, count in Counter(row.get("requested_date", "") for row in quality_rows).items()
            if value and count > 1
        ]
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "year": year,
            "through_date": max((row["end_date"] for row in planned if int(row["year"]) == year), default=None),
            "expected_half_month_batches": len(expected_keys),
            "uploaded_half_month_batches": len(uploaded_keys),
            "validated_half_month_batches": len(valid_keys),
            "missing_batches": missing_batches,
            "invalid_batches": invalid_batches,
            "quality_rows": len(quality_rows),
            "quality_status_counts": dict(sorted(statuses.items())),
            "canonical_daily_csv_files": len(daily_files),
            "rejected_daily_csv_files": len(rejected_files),
            "duplicate_report_dates": duplicate_dates,
            "independent_batch_checks": len(independent_checks),
            "archive_status": (
                "COMPLETE"
                if len(valid_keys) == len(expected_keys) and not duplicate_dates
                else "INCOMPLETE"
            ),
        }

        zip_path = output_dir / f"canonical-floorsheets-{year}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            root = f"canonical-floorsheets-{year}"
            for file_path in daily_files:
                archive.write(file_path, f"{root}/daily_csv/{file_path.name}")
            for file_path in rejected_files:
                archive.write(file_path, f"{root}/rejected_csv/{file_path.name}")
            if columns:
                archive.writestr(
                    f"{root}/quality_report_{year}.csv",
                    csv_bytes(quality_rows, columns),
                )
            archive.writestr(
                f"{root}/YEAR_MANIFEST.json",
                json.dumps(manifest, indent=2).encode("utf-8"),
            )
        print(f"Created {zip_path} | {manifest['archive_status']} | {len(daily_files)} usable CSVs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-year", required=True, type=int)
    parser.add_argument("--end-year", required=True, type=int)
    parser.add_argument(
        "--through-date",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        help="Final date used when the matrix was planned.",
    )
    args = parser.parse_args()
    assemble(
        args.input_dir,
        args.output_dir,
        args.start_year,
        args.end_year,
        args.through_date,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
