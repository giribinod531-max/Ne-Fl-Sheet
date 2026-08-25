#!/usr/bin/env python3
"""Run and package one validated daily NEPSE floorsheet and price download."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
USABLE_FLOORSHEET = {"COMPLETE", "MINOR_GAP"}


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use YYYY-MM-DD, for example 2026-08-25."
        ) from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected an object in {path}.")
    return value


def classify_overall(floorsheet_status: str, price_status: str) -> str:
    if floorsheet_status == "NOT_AVAILABLE" and price_status == "NOT_AVAILABLE":
        return "NO_DATA"
    if floorsheet_status == "COMPLETE" and price_status == "DOWNLOADED":
        return "READY"
    if floorsheet_status == "MINOR_GAP" and price_status == "DOWNLOADED":
        return "SOURCE_GAP"
    return "INCOMPLETE"


def run_command(arguments: list[str]) -> int:
    print("\nRunning:", " ".join(arguments), flush=True)
    completed = subprocess.run(arguments, cwd=PROJECT_DIR, check=False)
    return completed.returncode


def package_directory(source_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir.parent))
    temporary.replace(zip_path)


def run_daily(requested: date, output_root: Path, delay: float) -> dict[str, Any]:
    date_text = requested.isoformat()
    date_dir = output_root / date_text
    floorsheet_dir = date_dir / "floorsheet"
    price_dir = date_dir / "price"
    release_dir = output_root / "release_assets"
    date_dir.mkdir(parents=True, exist_ok=True)

    floor_return = run_command(
        [
            sys.executable,
            "floorsheet_downloader.py",
            "--start-date",
            date_text,
            "--end-date",
            date_text,
            "--output-dir",
            str(floorsheet_dir.resolve()),
            "--delay",
            str(delay),
        ]
    )
    price_return = run_command(
        [
            sys.executable,
            "price_csv_downloader.py",
            "--date",
            date_text,
            "--output-dir",
            str(price_dir.resolve()),
        ]
    )

    floorsheet_summary_path = floorsheet_dir / "validation_summary.json"
    price_status_path = price_dir / "price_status.json"

    try:
        floorsheet_summary = read_json(floorsheet_summary_path)
        floor_results = floorsheet_summary.get("results")
        if not isinstance(floor_results, list) or len(floor_results) != 1:
            raise RuntimeError("The floorsheet summary must contain exactly one date.")
        floor_result = floor_results[0]
        if not isinstance(floor_result, dict):
            raise RuntimeError("The floorsheet result has an unexpected format.")
        floorsheet_status = str(floor_result.get("status", "FAILED"))
    except RuntimeError as exc:
        floorsheet_summary = {"error": str(exc)}
        floorsheet_status = "FAILED"

    try:
        price_summary = read_json(price_status_path)
        price_status = str(price_summary.get("status", "FAILED"))
    except RuntimeError as exc:
        price_summary = {"error": str(exc)}
        price_status = "FAILED"

    overall_status = classify_overall(floorsheet_status, price_status)
    daily_status = {
        "requested_date": date_text,
        "overall_status": overall_status,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "floorsheet_status": floorsheet_status,
        "price_status": price_status,
        "floorsheet_return_code": floor_return,
        "price_return_code": price_return,
        "floorsheet": floorsheet_summary,
        "price": price_summary,
    }
    status_path = date_dir / "daily_status.json"
    status_path.write_text(json.dumps(daily_status, indent=2), encoding="utf-8")

    zip_path = release_dir / f"nepse-data-{date_text}.zip"
    package_directory(date_dir, zip_path)
    if overall_status == "NO_DATA":
        marker_path = release_dir / f"nepse-no-data-{date_text}.json"
        shutil.copy2(status_path, marker_path)

    print("\nDaily result")
    print("Date:", date_text)
    print("Floorsheet:", floorsheet_status)
    print("Price:", price_status)
    print("Overall:", overall_status)
    print("Package:", zip_path)
    return daily_status


def self_test() -> int:
    assert classify_overall("COMPLETE", "DOWNLOADED") == "READY"
    assert classify_overall("MINOR_GAP", "DOWNLOADED") == "SOURCE_GAP"
    assert classify_overall("NOT_AVAILABLE", "NOT_AVAILABLE") == "NO_DATA"
    assert classify_overall("REJECT", "DOWNLOADED") == "INCOMPLETE"
    assert classify_overall("COMPLETE", "FAILED") == "INCOMPLETE"
    print("Daily runner self-test passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=parse_iso_date)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return self_test()
    if args.date is None:
        print("--date is required unless --self-test is used.", file=sys.stderr)
        return 2
    if args.delay < 1.0:
        print("The daily delay must be at least 1.0 second.", file=sys.stderr)
        return 2

    result = run_daily(args.date, args.output_dir, args.delay)
    return 1 if result["overall_status"] == "INCOMPLETE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
