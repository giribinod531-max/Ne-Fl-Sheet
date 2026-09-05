#!/usr/bin/env python3
"""Merge old validated batches with Stage 05E recovery results without duplicates."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from canonical_05e_plan import read_config, recovery_batches
BATCH_PATTERN = re.compile(r"canonical-half-(\d{4}-\d{2}-P[12])$")


def batch_check(batch_dir: Path) -> tuple[bool, str]:
    path = batch_dir / "independent_validation.json"
    if not path.is_file():
        return False, "independent_validation.json is missing"
    try:
        check = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"independent validation cannot be read: {exc}"
    if check.get("result") != "PASS":
        return False, "independent validation did not pass"
    if int((check.get("status_counts") or {}).get("FAILED", 0)):
        return False, "batch contains technically failed dates"
    if not (batch_dir / "quality_report.csv").is_file():
        return False, "quality_report.csv is missing"
    return True, "validated"


def discover(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("canonical-half-*")):
        if not path.is_dir():
            continue
        match = BATCH_PATTERN.fullmatch(path.name)
        if not match:
            continue
        batch_id = match.group(1)
        if batch_id in found:
            raise ValueError(
                f"Duplicate {batch_id} inside {root}: {found[batch_id]} and {path}"
            )
        found[batch_id] = path
    return found


def copy_batch(source: Path, output_dir: Path, batch_id: str) -> None:
    destination = output_dir / f"canonical-half-{batch_id}"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def merge(
    baseline_dirs: list[Path],
    recovery_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    baseline: dict[str, Path] = {}
    for root in baseline_dirs:
        for batch_id, path in discover(root).items():
            if batch_id in baseline:
                raise ValueError(f"Baseline contains duplicate batch {batch_id}.")
            baseline[batch_id] = path
    recovered = discover(recovery_dir)
    target_ids = {str(row["batch_id"]) for row in recovery_batches(config)}
    unexpected = sorted(set(recovered) - target_ids)
    if unexpected:
        raise ValueError(f"Recovery contains unexpected batches: {unexpected}")

    if output_dir.exists():
        if output_dir.resolve() in {Path("/").resolve(), Path.cwd().resolve()}:
            raise ValueError("Refusing to replace an unsafe output directory.")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    source_for: dict[str, str] = {}
    for batch_id, source in sorted(baseline.items()):
        copy_batch(source, output_dir, batch_id)
        source_for[batch_id] = "baseline"
    for batch_id, source in sorted(recovered.items()):
        copy_batch(source, output_dir, batch_id)
        source_for[batch_id] = "05E recovery"

    valid_baseline_missing: list[str] = []
    valid_baseline_invalid: list[dict[str, str]] = []
    for batch_id in config["valid_batches"]:
        path = output_dir / f"canonical-half-{batch_id}"
        if not path.is_dir():
            valid_baseline_missing.append(batch_id)
            continue
        valid, reason = batch_check(path)
        if not valid:
            valid_baseline_invalid.append({"batch_id": batch_id, "reason": reason})

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_batches_found": len(baseline),
        "recovery_targets": len(target_ids),
        "recovery_batches_found": len(recovered),
        "missing_recovery_batches": sorted(target_ids - set(recovered)),
        "valid_baseline_missing": valid_baseline_missing,
        "valid_baseline_invalid": valid_baseline_invalid,
        "selected_source": dict(sorted(source_for.items())),
    }
    (output_dir / "MERGE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if valid_baseline_missing or valid_baseline_invalid:
        raise RuntimeError("A previously validated baseline batch is unavailable or invalid.")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", action="append", required=True, type=Path)
    parser.add_argument("--recovery-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    manifest = merge(
        args.baseline_dir,
        args.recovery_dir,
        args.output_dir,
        read_config(args.config),
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
