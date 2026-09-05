from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from canonical_05e_plan import balance_batches, read_config, recovery_batches
from canonical_merge_batches import merge
from canonical_verify_year_packages import verify


ROOT = Path(__file__).parents[1]


class RecoveryPlanTests(unittest.TestCase):
    def test_audited_baseline_leaves_exactly_117_recovery_batches(self) -> None:
        config = read_config(ROOT / "canonical_05e_targets.json")
        targets = recovery_batches(config)
        self.assertEqual(len(config["valid_batches"]), 43)
        self.assertEqual(len(targets), 117)
        self.assertEqual(len({row["batch_id"] for row in targets}), 117)

    def test_largest_processing_balances_and_labels_twenty_workers(self) -> None:
        batches = [
            {"batch_id": f"test-{value}", "estimated_transactions": value}
            for value in range(1, 41)
        ]
        matrix, workers = balance_batches(batches, 20)
        self.assertEqual(len(matrix), 40)
        self.assertEqual({row["worker"] for row in matrix}, {f"05E-{n}" for n in range(1, 21)})
        self.assertEqual(len({row["batch_id"] for row in matrix}), 40)
        loads = [row["estimated_transactions"] for row in workers]
        self.assertLessEqual(max(loads) - min(loads), 1)

    def test_workflow_uses_twenty_parallel_isolated_half_month_jobs(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "05e-balanced-canonical-recovery.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("max-parallel: 20", workflow)
        self.assertIn("matrix.batch.worker", workflow)
        self.assertIn("run-id: 33422133338", workflow)
        self.assertIn("run-id: 33415523179", workflow)
        self.assertIn("Publish only a complete audited release", workflow)


class MergeTests(unittest.TestCase):
    @staticmethod
    def make_batch(root: Path, batch_id: str, marker: str) -> Path:
        batch = root / f"canonical-half-{batch_id}"
        batch.mkdir(parents=True)
        (batch / "quality_report.csv").write_text(
            "requested_date,status\n2020-01-01,NOT_AVAILABLE\n", encoding="utf-8"
        )
        (batch / "independent_validation.json").write_text(
            json.dumps({"result": "PASS", "status_counts": {"FAILED": 0}}),
            encoding="utf-8",
        )
        (batch / "marker.txt").write_text(marker, encoding="utf-8")
        return batch

    def test_recovery_overlays_baseline_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "baseline"
            recovered = root / "recovered"
            self.make_batch(baseline, "2020-01-P1", "old")
            self.make_batch(recovered, "2020-01-P1", "new")
            config = {
                "start_year": 2020,
                "end_year": 2020,
                "through_date": "2020-01-15",
                "worker_count": 1,
                "baseline_runs": {},
                "valid_batches": [],
            }
            output = root / "combined"
            manifest = merge([baseline], recovered, output, config)
            self.assertEqual(
                (output / "canonical-half-2020-01-P1" / "marker.txt").read_text(),
                "new",
            )
            self.assertEqual(manifest["recovery_batches_found"], 1)


class FinalAuditTests(unittest.TestCase):
    def test_incomplete_manifest_fails_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "canonical-floorsheets-2020.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "canonical-floorsheets-2020/YEAR_MANIFEST.json",
                    json.dumps(
                        {
                            "archive_status": "INCOMPLETE",
                            "missing_batches": ["2020-01-P1"],
                            "invalid_batches": [],
                            "duplicate_report_dates": [],
                            "quality_status_counts": {},
                        }
                    ),
                )
            result = verify(root, 2020, 2020)
            self.assertEqual(result["result"], "FAIL")
            self.assertTrue(result["failures"])


if __name__ == "__main__":
    unittest.main()
