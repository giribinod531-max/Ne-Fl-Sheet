from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from canonical_assemble_years import assemble
from canonical_backfill_plan import build_plan
from canonical_floorsheet_downloader import (
    Transaction,
    broker_value_is_valid,
    classify_quality,
    parse_integer,
)
from canonical_validate_batch import (
    CANONICAL_COLUMNS,
    ValidationError,
    run,
    validate_csv,
)


QUALITY_COLUMNS = [
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
        self.assertIn("logs/console.log", workflow)


class HistoricalBrokerMarkerTests(unittest.TestCase):
    @staticmethod
    def transaction(number: int, buyer: str = "1") -> Transaction:
        return Transaction(
            transaction_no=f"202301010000{number:04d}",
            symbol="TEST",
            buyer=buyer,
            seller="2",
            quantity=1,
            rate=Decimal("1"),
            amount=Decimal("1"),
            source_page=11,
        )

    def test_invalid_integer_is_a_controlled_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_integer("-")

    def test_d01_is_a_valid_nepse_dealer_code(self) -> None:
        self.assertTrue(broker_value_is_valid("D01"))

    def test_d01_does_not_create_a_quality_gap(self) -> None:
        rows = [self.transaction(number, buyer="D01") for number in range(1, 11)]
        status, _ = classify_quality(10, 10, Decimal("10"), rows, [])
        self.assertEqual(status, "COMPLETE")

    def test_one_preserved_broker_marker_in_1000_rows_is_minor_gap(self) -> None:
        rows = [self.transaction(number) for number in range(1, 1001)]
        rows[0] = self.transaction(1, buyer="-")
        status, message = classify_quality(1000, 1000, Decimal("1000"), rows, [])
        self.assertEqual(status, "MINOR_GAP")
        self.assertIn("broker identifier", message)

    def test_broker_marker_above_point_one_percent_is_rejected(self) -> None:
        rows = [self.transaction(number) for number in range(1, 501)]
        rows[0] = self.transaction(1, buyer="-")
        status, _ = classify_quality(500, 500, Decimal("500"), rows, [])
        self.assertEqual(status, "REJECT")


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

    def test_non_numeric_broker_is_preserved_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2023-01-01.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CANONICAL_COLUMNS)
                writer.writerow(
                    [1, "20230101000001", "TEST", "-", 2, 10, "25.5", "255", "2023-01-01"]
                )
            result = validate_csv(path, "2023-01-01", 1)
            self.assertEqual(result["broker_anomaly_rows"], 1)

    def test_d01_is_preserved_as_a_valid_dealer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2023-01-01.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CANONICAL_COLUMNS)
                writer.writerow(
                    [1, "20230101000001", "TEST", "D01", 2, 10, "25.5", "255", "2023-01-01"]
                )
            result = validate_csv(path, "2023-01-01", 1)
            self.assertEqual(result["broker_anomaly_rows"], 0)

    def test_duplicate_transaction_ids_are_counted_not_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2023-01-01.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CANONICAL_COLUMNS)
                writer.writerow(
                    [1, "20230101000001", "AAA", 1, 2, 10, "25", "250", "2023-01-01"]
                )
                writer.writerow(
                    [2, "20230101000001", "BBB", 3, 4, 20, "30", "600", "2023-01-01"]
                )
            result = validate_csv(path, "2023-01-01", 2)
            self.assertEqual(result["duplicate_transaction_id_rows"], 1)

    def test_historical_six_significant_digit_amount_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2020-08-19.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CANONICAL_COLUMNS)
                writer.writerow(
                    [1, "2020081901003685", "TEST", 48, 48, 55191, 251, 13852900, "2020-08-19"]
                )
            result = validate_csv(path, "2020-08-19", 1)
            self.assertEqual(result["source_rounded_amount_rows"], 1)
            self.assertEqual(result["maximum_amount_rounding_difference"], "41")

    def test_amount_beyond_historical_display_precision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2020-08-19.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CANONICAL_COLUMNS)
                writer.writerow(
                    [1, "2020081901003685", "TEST", 48, 48, 55191, 251, 13852800, "2020-08-19"]
                )
            with self.assertRaises(ValidationError):
                validate_csv(path, "2020-08-19", 1)

    def test_duplicate_transaction_ids_force_reject(self) -> None:
        rows = [
            Transaction("20230101000001", "AAA", "1", "2", 10, Decimal("25"), Decimal("250"), 1),
            Transaction("20230101000001", "BBB", "3", "4", 20, Decimal("30"), Decimal("600"), 1),
        ]
        status, message = classify_quality(2, 30, Decimal("850"), rows, [])
        self.assertEqual(status, "REJECT")
        self.assertIn("duplicate", message)


class AssemblyTests(unittest.TestCase):
    def test_incomplete_year_is_explicitly_marked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = root / "collected" / "canonical-half-2014-01-P1"
            batch.mkdir(parents=True)
            (batch / "quality_report.csv").write_text(
                "requested_date,status\n2014-01-01,NOT_AVAILABLE\n", encoding="utf-8"
            )
            (batch / "independent_validation.json").write_text(
                json.dumps(
                    {
                        "result": "PASS",
                        "status_counts": {"FAILED": 0},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "yearly"
            assemble(root / "collected", output, 2014, 2014, date(2014, 12, 31))
            archive_path = output / "canonical-floorsheets-2014.zip"
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(
                    archive.read("canonical-floorsheets-2014/YEAR_MANIFEST.json")
                )
            self.assertEqual(manifest["archive_status"], "INCOMPLETE")
            self.assertEqual(manifest["uploaded_half_month_batches"], 1)
            self.assertEqual(manifest["validated_half_month_batches"], 1)
            self.assertEqual(len(manifest["missing_batches"]), 23)

    def test_failed_batch_is_not_accepted_into_year_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = root / "collected" / "canonical-half-2026-01-P1"
            (batch / "daily_csv").mkdir(parents=True)
            (batch / "quality_report.csv").write_text(
                "requested_date,status\n2026-01-01,FAILED\n", encoding="utf-8"
            )
            (batch / "independent_validation.json").write_text(
                json.dumps(
                    {
                        "result": "PASS",
                        "status_counts": {"FAILED": 1},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "yearly"
            assemble(root / "collected", output, 2026, 2026, date(2026, 1, 15))
            with zipfile.ZipFile(output / "canonical-floorsheets-2026.zip") as archive:
                manifest = json.loads(
                    archive.read("canonical-floorsheets-2026/YEAR_MANIFEST.json")
                )
            self.assertEqual(manifest["expected_half_month_batches"], 1)
            self.assertEqual(manifest["uploaded_half_month_batches"], 1)
            self.assertEqual(manifest["validated_half_month_batches"], 0)
            self.assertEqual(manifest["archive_status"], "INCOMPLETE")
            self.assertEqual(manifest["invalid_batches"][0]["batch_id"], "2026-01-P1")

    def test_current_year_does_not_expect_future_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "yearly"
            assemble(root / "collected", output, 2026, 2026, date(2026, 8, 30))
            with zipfile.ZipFile(output / "canonical-floorsheets-2026.zip") as archive:
                manifest = json.loads(
                    archive.read("canonical-floorsheets-2026/YEAR_MANIFEST.json")
                )
            self.assertEqual(manifest["expected_half_month_batches"], 16)
            self.assertEqual(manifest["through_date"], "2026-08-30")


if __name__ == "__main__":
    unittest.main()
