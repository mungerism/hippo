"""CLI runner for Jev relationship classification calibration (#64).

Usage:
  # Replay offline evaluation and produce calibration artifact
  uv run python benchmarks/jev_calibration.py \
    --dataset benchmarks/jev_pairs_v1.json \
    --recordings benchmarks/jev_recordings_synthetic_v1.json \
    --output benchmarks/reports/calibration_jev_v1.json

  # Optionally run live against TypeSafe API if key is available
  uv run python benchmarks/jev_calibration.py \
    --dataset benchmarks/jev_pairs_v1.json \
    --live-api
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow direct invocation from repository root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippo_memory.jev_calibration import (
    CalibrationGatePolicy,
    calibrate,
    run_live_benchmark,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Jev Classification Benchmark & Calibration")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/jev_pairs_v1.json"),
        help="Path to pair dataset JSON (default: benchmarks/jev_pairs_v1.json)",
    )
    parser.add_argument(
        "--recordings",
        type=Path,
        default=Path("benchmarks/jev_recordings_synthetic_v1.json"),
        help="Path to recordings JSON (default: benchmarks/jev_recordings_synthetic_v1.json)",
    )
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="Execute live requests against Jev API using TYPESAFE_API_KEY",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="TypeSafe API key (defaults to TYPESAFE_API_KEY env var)",
    )
    parser.add_argument(
        "--min-test-samples",
        type=int,
        default=30,
        help="Minimum test split sample count for RFC safety gate (default: 30)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write calibration artifact JSON",
    )
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))

    if args.live_api:
        api_key = args.api_key or os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            sys.stderr.write("Error: --live-api requires --api-key or TYPESAFE_API_KEY env var\n")
            sys.exit(1)
        sys.stderr.write("Running live benchmark against Jev API...\n")
        recordings = run_live_benchmark(dataset, api_key=api_key)
    else:
        recordings = json.loads(args.recordings.read_text(encoding="utf-8"))

    gate_policy = CalibrationGatePolicy(min_test_samples=args.min_test_samples)
    artifact = calibrate(dataset, recordings, gate_policy=gate_policy)

    # Print summary to stderr
    status = artifact["status"]
    sys.stderr.write(f"\n=== Calibration Status: {status} ===\n")
    if not artifact["qualification"]["is_qualified_for_takeover"]:
        sys.stderr.write("Disqualification reasons:\n")
        for reason in artifact["qualification"]["disqualification_reasons"]:
            sys.stderr.write(f"  - {reason}\n")
    else:
        sys.stderr.write("Qualified for takeover: YES\n")

    test_overall = artifact["metrics"]["test_split"]["overall"]
    sys.stderr.write(
        f"Test split: count={test_overall['count']}, accepted={test_overall['accepted']}, "
        f"coverage={test_overall['coverage']}, false_merge={test_overall['false_merge']}, "
        f"false_supersede={test_overall['false_supersede']}, "
        f"error_upper_bound_95={test_overall['error_upper_bound_95']}\n"
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.stderr.write(f"Wrote calibration artifact to {args.output}\n")
    else:
        json.dump(artifact, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
