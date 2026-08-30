from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from canonical_assemble_years import assemble
from canonical_backfill_plan import build_plan
from canonical_validate_batch import CANONICAL_COLUMNS, ValidationError, run


QUALITY_COLUMNS = [
    "requested_date",
    "market_date",
    "status",
    "pages",
    "expected_records",
    "downloaded_unique_rows",
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


class PlanTests(unittest.TestCase):
    def test_three_year_plan_has_72_half_month_jobs(self) -> None:
        plan = build_plan(2014, 2016, date(2026, 8, 30))
        self.assertEqual(len(plan), 72)
        self.assertEqual(plan[0]["start_date"], "2014-01-01")
        self.assertEqual(plan[-1]["end_date"], "2016-12-31")

    def test_current_year_is_capped_at_through_date(self) -> None:
        plan = build_plan(2026, 2026, date(2026, 8, 30))
        self.assertEqual(len(plan), 16)
        self.assertEqual(plan[-1]["start_date"], "2026-08-16")
        self.assertEqual(plan[-1]["end_date"], "2026-08-30")

    def test_leap_day_is_in_second_half(self) -> None:
        plan = build_plan(2016, 2016, date(2016, 12, 31))
        february_second = next(row for row in plan if row["batch_id"] == "2016-02-P2")
        self.assertEqual(february_second["end_date"], "2016-02-29")

    def test_worker_fails_only_for_technical_or_audit_failure(self) -> None:
        workflow = (
            Path(__file__).parents[1]
            / ".github"
            / "workflows"
            / "_05-canonical-half-month-worker.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("SCRAPE_OUTCOME", workflow)
        self.assertIn("AUDIT_OUTCOME", workflow)
        self.assertIn("status_counts']['FAILED']", workflow)


class OutputValidationTests(unittest.TestCase):
    def make_output(self, root: Path) -> None:
        (root / "daily_csv").mkdir(parents=True)
        with (root / "daily_csv" / "2026-08-20.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(CANONICAL_COLUMNS)
            writer.writerow(
                [1, "20260820000001", "TEST", 1, 2, 10, "25.50", "255.00", "2026-08-20"]
            )

        rows = [
            {
                "requested_date": "2026-08-20",
                "market_date": "2026-08-20",
                "status": "COMPLETE",
                "pages": "1",
                "expected_records": "1",
                "downloaded_unique_rows": "1",
                "missing_rows": "0",
                "missing_rows_percent": "0",
                "expected_quantity": "10",
                "downloaded_quantity": "10",
                "missing_quantity": "0",
                "missing_quantity_percent": "0",
                "expected_amount": "255",
                "downloaded_amount": "255",
                "missing_amount": "0",
                "missing_amount_percent": "0",
                "short_page_numbers": "",
                "message": "Complete",
            },
            {
                "requested_date": "2026-08-21",
                "market_date": "",
                "status": "NOT_AVAILABLE",
                "pages": "0",
                "expected_records": "0",
                "downloaded_unique_rows": "0",
                "missing_rows": "0",
                "missing_rows_percent": "0",
                "expected_quantity": "0",
                "downloaded_quantity": "0",
                "missing_quantity": "0",
                "missing_quantity_percent": "0",
                "expected_amount": "0",
                "downloaded_amount": "0",
                "missing_amount": "0",
                "missing_amount_percent": "0",
                "short_page_numbers": "",
                "message": "No data",
            },
        ]
        with (root / "quality_report.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=QUALITY_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        summary = {
            "status_counts": {
                "COMPLETE": 1,
                "MINOR_GAP": 0,
                "REJECT": 0,
                "NOT_AVAILABLE": 1,
                "FAILED": 0,
            },
            "total_downloaded_rows": 1,
        }
        (root / "validation_summary.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_canonical_batch_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_output(root)
            result = run(root, date(2026, 8, 20), date(2026, 8, 21))
            self.assertEqual(result["result"], "PASS")
            self.assertEqual(result["validated_rows"], 1)

    def test_page_column_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_output(root)
            path = root / "daily_csv" / "2026-08-20.csv"
            text = path.read_text(encoding="utf-8-sig")
            path.write_text(text.replace(",Date", ",Date,Page"), encoding="utf-8-sig")
            with self.assertRaises(ValidationError):
                run(root, date(2026, 8, 20), date(2026, 8, 21))


class AssemblyTests(unittest.TestCase):
    def test_incomplete_year_is_explicitly_marked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = root / "collected" / "canonical-half-2014-01-P1"
            batch.mkdir(parents=True)
            (batch / "quality_report.csv").write_text(
                "requested_date,status\n2014-01-01,NOT_AVAILABLE\n", encoding="utf-8"
            )
            output = root / "yearly"
            assemble(root / "collected", output, 2014, 2014)
            archive_path = output / "canonical-floorsheets-2014.zip"
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(
                    archive.read("canonical-floorsheets-2014/YEAR_MANIFEST.json")
                )
            self.assertEqual(manifest["archive_status"], "INCOMPLETE")
            self.assertEqual(manifest["available_half_month_batches"], 1)
            self.assertEqual(len(manifest["missing_batches"]), 23)


if __name__ == "__main__":
    unittest.main()
