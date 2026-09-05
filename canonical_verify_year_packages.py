#!/usr/bin/env python3
"""Verify that every assembled yearly canonical package is complete."""

from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def verify(package_dir: Path, start_year: int, end_year: int) -> dict[str, object]:
    years: list[dict[str, object]] = []
    failures: list[str] = []
    for year in range(start_year, end_year + 1):
        path = package_dir / f"canonical-floorsheets-{year}.zip"
        if not path.is_file():
            failures.append(f"{year}: ZIP is missing")
            continue
        member = f"canonical-floorsheets-{year}/YEAR_MANIFEST.json"
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read(member))
                bad_member = archive.testzip()
        except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            failures.append(f"{year}: cannot verify ZIP: {exc}")
            continue
        if bad_member:
            failures.append(f"{year}: corrupt member {bad_member}")
        if manifest.get("archive_status") != "COMPLETE":
            failures.append(f"{year}: archive is {manifest.get('archive_status')}")
        if manifest.get("missing_batches"):
            failures.append(f"{year}: missing half-month batches")
        if manifest.get("invalid_batches"):
            failures.append(f"{year}: invalid half-month batches")
        if manifest.get("duplicate_report_dates"):
            failures.append(f"{year}: duplicate report dates")
        if int((manifest.get("quality_status_counts") or {}).get("FAILED", 0)):
            failures.append(f"{year}: technically failed dates remain")
        years.append(manifest)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
        "years": years,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--start-year", required=True, type=int)
    parser.add_argument("--end-year", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = verify(args.package_dir, args.start_year, args.end_year)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
