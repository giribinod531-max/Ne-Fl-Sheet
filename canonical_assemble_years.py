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
from datetime import datetime, timezone
from pathlib import Path


BATCH_PATTERN = re.compile(r"canonical-half-(\d{4})-(\d{2})-P([12])$")


def csv_bytes(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def assemble(input_dir: Path, output_dir: Path, start_year: int, end_year: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_dirs: dict[tuple[int, int, int], Path] = {}
    for path in input_dir.rglob("canonical-half-*"):
        if not path.is_dir():
            continue
        match = BATCH_PATTERN.fullmatch(path.name)
        if match:
            key = tuple(int(value) for value in match.groups())
            batch_dirs[key] = path

    for year in range(start_year, end_year + 1):
        expected_keys = [(year, month, part) for month in range(1, 13) for part in (1, 2)]
        available_keys = [key for key in expected_keys if key in batch_dirs]
        missing_batches = [f"{y}-{m:02d}-P{p}" for y, m, p in expected_keys if key_missing((y,m,p), batch_dirs)]
        quality_rows: list[dict[str, str]] = []
        columns: list[str] | None = None
        daily_files: list[Path] = []
        rejected_files: list[Path] = []
        independent_checks: list[dict[str, object]] = []

        for key in available_keys:
            batch_dir = batch_dirs[key]
            report_path = batch_dir / "quality_report.csv"
            if report_path.is_file():
                with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    columns = columns or list(reader.fieldnames or [])
                    quality_rows.extend(reader)
            daily_files.extend(sorted((batch_dir / "daily_csv").glob("*.csv")))
            rejected_files.extend(sorted((batch_dir / "rejected_csv").glob("*.csv")))
            check_path = batch_dir / "independent_validation.json"
            if check_path.is_file():
                independent_checks.append(json.loads(check_path.read_text(encoding="utf-8")))

        quality_rows.sort(key=lambda row: row.get("requested_date", ""))
        statuses = Counter(row.get("status", "UNKNOWN") for row in quality_rows)
        duplicate_dates = [
            value for value, count in Counter(row.get("requested_date", "") for row in quality_rows).items()
            if value and count > 1
        ]
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "year": year,
            "expected_half_month_batches": 24,
            "available_half_month_batches": len(available_keys),
            "missing_batches": missing_batches,
            "quality_rows": len(quality_rows),
            "quality_status_counts": dict(sorted(statuses.items())),
            "canonical_daily_csv_files": len(daily_files),
            "rejected_daily_csv_files": len(rejected_files),
            "duplicate_report_dates": duplicate_dates,
            "independent_batch_checks": len(independent_checks),
            "archive_status": "COMPLETE" if not missing_batches and not duplicate_dates else "INCOMPLETE",
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


def key_missing(key: tuple[int, int, int], batches: dict[tuple[int, int, int], Path]) -> bool:
    return key not in batches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-year", required=True, type=int)
    parser.add_argument("--end-year", required=True, type=int)
    args = parser.parse_args()
    assemble(args.input_dir, args.output_dir, args.start_year, args.end_year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
