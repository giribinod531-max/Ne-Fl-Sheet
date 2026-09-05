#!/usr/bin/env python3
"""Build a transaction-balanced Stage 05E recovery matrix.

Only batches not already validated in the Stage 05C/05D baseline are planned.
Merolagani's displayed record count is probed without downloading every page;
the counts are used only for scheduling. The recovery downloader independently
re-reads and validates every total before any CSV is accepted.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from canonical_backfill_plan import build_plan
_THREAD_STATE = threading.local()


def read_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "start_year",
        "end_year",
        "through_date",
        "worker_count",
        "baseline_runs",
        "valid_batches",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"Recovery config is missing: {', '.join(missing)}")
    return config


def recovery_batches(config: dict[str, Any]) -> list[dict[str, str | int]]:
    through = datetime.strptime(str(config["through_date"]), "%Y-%m-%d").date()
    complete_plan = build_plan(
        int(config["start_year"]), int(config["end_year"]), through
    )
    planned_ids = {str(row["batch_id"]) for row in complete_plan}
    valid = [str(value) for value in config["valid_batches"]]
    if len(valid) != len(set(valid)):
        raise ValueError("valid_batches contains a duplicate batch ID.")
    unknown = sorted(set(valid) - planned_ids)
    if unknown:
        raise ValueError(f"Valid batch IDs are outside the plan: {unknown}")
    valid_set = set(valid)
    return [row for row in complete_plan if str(row["batch_id"]) not in valid_set]


def iter_batch_dates(batch: dict[str, str | int]) -> list[date]:
    start = datetime.strptime(str(batch["start_date"]), "%Y-%m-%d").date()
    end = datetime.strptime(str(batch["end_date"]), "%Y-%m-%d").date()
    values: list[date] = []
    current = start
    while current <= end:
        values.append(current)
        current += timedelta(days=1)
    return values


def thread_session() -> Any:
    import canonical_floorsheet_downloader as floorsheet

    session = getattr(_THREAD_STATE, "session", None)
    if session is None:
        session = floorsheet.build_session()
        _THREAD_STATE.session = session
    return session


def probe_date(requested: date, delay_seconds: float) -> tuple[int, str]:
    """Return Merolagani's displayed transaction count for one date."""
    import canonical_floorsheet_downloader as floorsheet

    session = thread_session()
    soup = floorsheet.request_html(session, "GET")
    fields = floorsheet.read_form_fields(soup)
    fields["__EVENTTARGET"] = floorsheet.SEARCH_EVENT
    fields["__EVENTARGUMENT"] = ""
    fields[floorsheet.DATE_FIELD] = requested.strftime("%m/%d/%Y")
    time.sleep(delay_seconds)
    soup = floorsheet.request_html(session, "POST", fields)

    market_text, market_date = floorsheet.extract_market_date(soup)
    if not floorsheet.extract_transactions(soup, 1):
        return 0, ""
    if market_date != requested:
        raise floorsheet.FloorsheetError(
            f"Merolagani returned {market_text!r} for {requested.isoformat()}."
        )
    pager = floorsheet.extract_pager_stats(soup)
    if pager.total_records is None or pager.total_records <= 0:
        raise floorsheet.FloorsheetError(
            f"Could not read transaction count: {pager.text!r}"
        )
    return pager.total_records, ""


def probe_workload(
    batches: list[dict[str, str | int]], concurrency: int, delay_seconds: float
) -> tuple[dict[str, dict[str, float | int]], list[dict[str, str | int]]]:
    all_dates = sorted(
        {requested for batch in batches for requested in iter_batch_dates(batch)}
    )
    results: dict[date, tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_dates = {
            executor.submit(probe_date, requested, delay_seconds): requested
            for requested in all_dates
        }
        for position, future in enumerate(as_completed(future_dates), start=1):
            requested = future_dates[future]
            try:
                results[requested] = future.result()
            except Exception as exc:  # A scheduling estimate must not stop recovery.
                results[requested] = (0, f"{type(exc).__name__}: {exc}")
            if position % 50 == 0 or position == len(all_dates):
                print(
                    f"Probed {position}/{len(all_dates)} dates for workload planning.",
                    file=sys.stderr,
                    flush=True,
                )

    known_batch_totals: list[int] = []
    raw: dict[str, dict[str, float | int]] = {}
    detail_rows: list[dict[str, str | int]] = []
    for batch in batches:
        batch_id = str(batch["batch_id"])
        values = [results[requested] for requested in iter_batch_dates(batch)]
        counts = [count for count, _ in values if count > 0]
        errors = [message for _, message in values if message]
        known_total = sum(counts)
        if known_total:
            known_batch_totals.append(known_total)
        raw[batch_id] = {
            "known_total": known_total,
            "active_dates": len(counts),
            "probe_errors": len(errors),
        }
        for requested in iter_batch_dates(batch):
            count, error = results[requested]
            detail_rows.append(
                {
                    "batch_id": batch_id,
                    "date": requested.isoformat(),
                    "displayed_transactions": count,
                    "probe_error": error,
                }
            )

    fallback_total = int(statistics.median(known_batch_totals)) if known_batch_totals else 1
    workload: dict[str, dict[str, float | int]] = {}
    for batch_id, values in raw.items():
        known_total = int(values["known_total"])
        active_dates = int(values["active_dates"])
        error_count = int(values["probe_errors"])
        if known_total and error_count:
            estimate = known_total + round((known_total / active_dates) * error_count)
        else:
            estimate = known_total or fallback_total
        workload[batch_id] = {
            "estimated_transactions": max(1, estimate),
            "average_transactions": round(known_total / active_dates, 2)
            if active_dates
            else 0.0,
            "active_dates": active_dates,
            "probe_errors": error_count,
        }
    return workload, detail_rows


def balance_batches(
    batches: list[dict[str, Any]], worker_count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if worker_count < 1:
        raise ValueError("worker_count must be positive.")
    workers = [
        {"worker": f"05E-{index}", "estimated_transactions": 0, "batches": []}
        for index in range(1, worker_count + 1)
    ]
    for batch in sorted(
        batches,
        key=lambda row: (-int(row["estimated_transactions"]), str(row["batch_id"])),
    ):
        worker = min(
            workers,
            key=lambda row: (
                int(row["estimated_transactions"]),
                len(row["batches"]),
                int(str(row["worker"]).split("-")[-1]),
            ),
        )
        assigned = {**batch, "worker": worker["worker"]}
        worker["batches"].append(assigned)
        worker["estimated_transactions"] += int(batch["estimated_transactions"])

    # Interleave workers so GitHub initially launches one job from every group.
    matrix: list[dict[str, Any]] = []
    longest = max((len(row["batches"]) for row in workers), default=0)
    for position in range(longest):
        for worker in workers:
            if position < len(worker["batches"]):
                matrix.append(worker["batches"][position])
    return matrix, workers


def write_outputs(
    output_dir: Path,
    matrix: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    details: list[dict[str, str | int]],
    config: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "matrix.json").write_text(
        json.dumps(matrix, separators=(",", ":")), encoding="utf-8"
    )
    (output_dir / "worker_plan.json").write_text(
        json.dumps(workers, indent=2), encoding="utf-8"
    )
    with (output_dir / "workload_probe.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        columns = ["batch_id", "date", "displayed_transactions", "probe_error"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(details)
    summary = {
        "through_date": config["through_date"],
        "baseline_runs": config["baseline_runs"],
        "already_valid_batches": len(config["valid_batches"]),
        "recovery_batches": len(matrix),
        "workers": len(workers),
        "worker_estimated_transactions": {
            row["worker"]: row["estimated_transactions"] for row in workers
        },
    }
    (output_dir / "plan_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--probe-concurrency", type=int, default=8)
    parser.add_argument("--probe-delay", type=float, default=0.5)
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Use equal weights; intended only for offline tests.",
    )
    args = parser.parse_args()
    if not 1 <= args.probe_concurrency <= 20:
        raise SystemExit("Probe concurrency must be between 1 and 20.")
    if args.probe_delay < 0.5:
        raise SystemExit("Probe delay must be at least 0.5 seconds.")

    config = read_config(args.config)
    batches = recovery_batches(config)
    worker_count = args.workers or int(config["worker_count"])
    if worker_count > 20:
        raise SystemExit("Stage 05E is capped at 20 concurrent GitHub-hosted workers.")
    if args.skip_probe:
        workload = {
            str(row["batch_id"]): {
                "estimated_transactions": 1,
                "average_transactions": 0.0,
                "active_dates": 0,
                "probe_errors": 0,
            }
            for row in batches
        }
        details: list[dict[str, str | int]] = []
    else:
        workload, details = probe_workload(
            batches, args.probe_concurrency, args.probe_delay
        )
    weighted = [{**row, **workload[str(row["batch_id"])]} for row in batches]
    matrix, workers = balance_batches(weighted, worker_count)
    write_outputs(args.output_dir, matrix, workers, details, config)
    print(json.dumps(matrix, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
